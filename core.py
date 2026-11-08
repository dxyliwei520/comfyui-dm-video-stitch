# -*- coding: utf-8 -*-
"""
dm_video_stitch.core —— 把多段视频「无痕」拼成一条，且**不解码成帧张量**（防 OOM）

设计要点（这就是为什么它不会 OOM）：
  1. 只拿每段视频的**源文件路径**，交给 ffmpeg 流式处理 —— 内存里从不出现 5 段的全部帧；
     （对比：GetVideoComponents + BatchImagesNode 会把 5 段解成 float32 IMAGE 张量，
      480x864x5 段 20 秒就要 ~12 GB，960x1728@96fps 更是 25 GB+）
  2. 每段先**归一化**（分辨率 / fps / pix_fmt / SAR），否则 xfade 拼不上（你的素材 960x1728@96 与 480x864@24 混着）；
  3. 段与段之间用 **xfade 交叉淡化**（视频）+ **acrossfade**（音频），接缝处是渐变而不是硬切，肉眼看不出断点；
  4. 全部在一个 ffmpeg 进程里一次做完：归一化 → 交叉淡化 → 编码 → 加 faststart。

作者：DSH（2026-11-08）｜无第三方依赖（只用系统 ffmpeg + PyAV 探测）
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Iterable, Optional

try:
    import av  # PyAV：ComfyUI 自带，用来探测时长/帧率/是否带音轨
except Exception:  # pragma: no cover
    av = None


# --------------------------------------------------------------------------------------
# ffmpeg 定位
# --------------------------------------------------------------------------------------
_FFMPEG_CANDIDATES = [
    "ffmpeg",
    r"C:\WINDOWS\system32\ffmpeg.exe",
    r"C:\ffmpeg\bin\ffmpeg.exe",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "ffmpeg", "bin", "ffmpeg.exe"),
]


def find_ffmpeg() -> str:
    for c in _FFMPEG_CANDIDATES:
        if not c:
            continue
        p = shutil.which(c) if os.path.basename(c) == c else (c if os.path.isfile(c) else None)
        if p:
            return p
    # 再在 PATH 里找一次（大小写/后缀差异）
    for name in ("ffmpeg.exe", "ffmpeg"):
        p = shutil.which(name)
        if p:
            return p
    raise RuntimeError("找不到 ffmpeg.exe（请把它放进 PATH，或把路径写进 DM_FFMPEG 环境变量）")


def ffmpeg_path() -> str:
    env = os.environ.get("DM_FFMPEG")
    if env and os.path.isfile(env):
        return env
    return find_ffmpeg()


# --------------------------------------------------------------------------------------
# 探测
# --------------------------------------------------------------------------------------
@dataclass
class SegInfo:
    path: str
    duration: float = 0.0
    fps: float = 24.0
    width: int = 0
    height: int = 0
    has_audio: bool = False
    sample_rate: int = 48000
    channels: int = 2
    trim_start: float = 0.0
    trim_duration: float = 0.0

    @property
    def eff_duration(self) -> float:
        d = self.duration
        if self.trim_duration and self.trim_duration > 0:
            d = min(d - self.trim_start, self.trim_duration) if d else self.trim_duration
        elif self.trim_start:
            d = max(d - self.trim_start, 0.0)
        return max(d, 0.0)


def probe_segment(path: str, trim_start: float = 0.0, trim_duration: float = 0.0) -> SegInfo:
    """用 PyAV 读真实参数（不依赖 ffprobe，D 盘这套没有 ffprobe）。"""
    if av is None:
        raise RuntimeError("PyAV 不可用，无法探测视频参数")
    info = SegInfo(path=path, trim_start=float(trim_start or 0.0), trim_duration=float(trim_duration or 0.0))
    with av.open(path) as c:
        vs = [s for s in c.streams if s.type == "video"]
        aus = [s for s in c.streams if s.type == "audio"]
        if not vs:
            raise RuntimeError(f"没有视频轨: {path}")
        v = vs[0]
        info.width = int(v.codec_context.width or 0)
        info.height = int(v.codec_context.height or 0)
        try:
            info.fps = float(v.average_rate) if v.average_rate else 24.0
        except Exception:
            info.fps = 24.0
        if info.fps <= 0.1:
            info.fps = 24.0
        if c.duration:
            info.duration = float(c.duration) / av.time_base
        elif v.duration and v.time_base:
            info.duration = float(v.duration * v.time_base)
        if aus:
            a = aus[0]
            info.has_audio = a.codec_context is not None
            info.sample_rate = int(a.codec_context.sample_rate or 48000) if a.codec_context else 48000
            info.channels = int(a.codec_context.channels or 2) if a.codec_context else 2
    return info


# --------------------------------------------------------------------------------------
# 拼装
# --------------------------------------------------------------------------------------
TRANSITIONS = ["none", "fade", "fadeblack", "fadegrays", "dissolve", "wipeleft", "wiperight", "slideleft", "circleopen"]
FITS = ["crop", "pad", "stretch"]
AUDIO_MODES = ["crossfade", "concat", "first_only", "silent", "none"]
PIX_FMTS = ["yuv420p", "yuv420p10le"]
# 段间衔接方式：
#   drop      = SCAIL2 式：生成时段间已经共享 previous_frame_count 帧，拼接只要**裁掉重复帧**再硬接
#               （内容本身连续 → 既看不出接缝，也不会出现淡化造成的重影）
#   crossfade = 通用式：段间没有关联时，用 xfade + acrossfade 掩盖硬切
#   none      = 纯硬接（段间无重叠又不想淡化时用，接缝可能可见）
OVERLAP_MODES = ["drop", "crossfade", "none"]


@dataclass
class StitchOptions:
    transition: str = "fade"
    transition_seconds: float = 0.35
    target_fps: float = 0.0            # 0 = 用第一段的帧率
    width: int = 0                     # 0 = 用第一段（按 fit 规则）
    height: int = 0
    fit: str = "crop"                  # crop=裁切填满（不变形）/ pad=加黑边 / stretch=拉伸
    pix_fmt: str = "yuv420p"
    audio_mode: str = "crossfade"      # crossfade / concat / first_only / silent / none
    audio_bitrate: str = "192k"
    crf: int = 18
    preset: str = "fast"
    audio_override: Optional[str] = None   # 用一条完整音轨覆盖（wav/mp3 路径）
    # ↓ SCAIL2 式衔接：段间共享的帧数（WanSCAILToVideo 的 previous_frame_count）
    overlap_frames: int = 0
    overlap_mode: str = "drop"


def _fit_filter(w: int, h: int, fit: str) -> str:
    if fit == "stretch":
        return f"scale={w}:{h}:flags=lanczos"
    if fit == "pad":
        return (f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos,"
                f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black")
    # crop（默认）
    return (f"scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={w}:{h}")


def build_command(segs: list[SegInfo], out_path: str, opt: StitchOptions) -> tuple[list[str], dict]:
    """返回 (ffmpeg 参数列表, 运行信息)。纯函数，便于单测。"""
    if not segs:
        raise ValueError("没有输入视频")
    n = len(segs)
    fps = opt.target_fps if opt.target_fps and opt.target_fps > 0 else round(segs[0].fps, 6)
    W = opt.width if opt.width and opt.width > 0 else (segs[0].width // 2 * 2)
    H = opt.height if opt.height and opt.height > 0 else (segs[0].height // 2 * 2)
    if W <= 0 or H <= 0:
        raise ValueError(f"分辨率无效: {W}x{H}")

    use_xfade = opt.transition != "none" and float(opt.transition_seconds) > 0 and n > 1
    d = float(opt.transition_seconds) if use_xfade else 0.0

    # 每段时长（归一化后仍按原时长）；xfade 要求 d 小于任一段时长的一半左右，做个保护
    durs = [s.eff_duration for s in segs]
    if use_xfade:
        dmax = min(durs) / 2.0 if min(durs) > 0 else 0.0
        if dmax > 0 and d > dmax:
            d = round(dmax, 3)
        if d <= 0.001:
            use_xfade, d = False, 0.0

    args: list[str] = [ffmpeg_path(), "-hide_banner", "-y", "-nostdin"]
    # 输入（带 trim）
    for s in segs:
        if s.trim_start > 0:
            args += ["-ss", f"{s.trim_start:.3f}"]
        if s.trim_duration > 0:
            args += ["-t", f"{s.trim_duration:.3f}"]
        args += ["-i", s.path]

    # 可选：外部整条音轨
    override_idx = None
    if opt.audio_override and os.path.isfile(opt.audio_override):
        override_idx = n
        args += ["-i", opt.audio_override]

    fps_s = f"{fps:.6f}".rstrip("0").rstrip(".") if fps % 1 else str(int(fps))
    fc: list[str] = []
    # 视频：归一化
    for i in range(n):
        fc.append(
            f"[{i}:v]trim=start=0,fps={fps_s},{_fit_filter(W, H, opt.fit)},setsar=1,"
            f"format={opt.pix_fmt},setpts=PTS-STARTPTS[v{i}]"
        )
    # 视频：交叉淡化链
    total = durs[0]
    if use_xfade:
        last = "v0"
        for i in range(1, n):
            off = total - d
            if off < 0:
                off = 0.0
            fc.append(f"[{last}][v{i}]xfade=transition={opt.transition}:duration={d:.3f}:offset={off:.3f}[vx{i}]")
            last = f"vx{i}"
            total = total + durs[i] - d
        vout = last
    else:
        fc.append("".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[vcat]")
        vout = "vcat"
        total = sum(durs)

    # 音频
    want_audio = opt.audio_mode != "none" and override_idx is None
    aout = None
    if override_idx is not None:
        # 外部整条音轨：先 apad 补静音，再 atrim 到视频总长 —— 音轨比视频短时不会"后半段没声音"
        fc.append(f"[{override_idx}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                  f"asetpts=PTS-STARTPTS,apad,atrim=0:{total:.3f}[ao]")
        aout = "ao"
    elif want_audio:
        if opt.audio_mode == "silent":
            fc.append(f"anullsrc=r=48000:cl=stereo:d={total:.3f}[ao]")
            aout = "ao"
        elif opt.audio_mode == "first_only":
            if segs[0].has_audio:
                fc.append(f"[0:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                          f"asetpts=PTS-STARTPTS,apad,atrim=0:{total:.3f}[ao]")
            else:
                fc.append(f"anullsrc=r=48000:cl=stereo:d={total:.3f}[ao]")
            aout = "ao"
        else:
            # concat / crossfade：先把每段音频规整成 48k 立体声；没音轨的补静音
            for i, s in enumerate(segs):
                if s.has_audio:
                    fc.append(f"[{i}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                              f"asetpts=PTS-STARTPTS,apad,atrim=0:{durs[i]:.3f}[a{i}]")
                else:
                    fc.append(f"anullsrc=r=48000:cl=stereo:d={durs[i]:.3f},"
                              f"aformat=sample_fmts=fltp:channel_layouts=stereo,asetpts=PTS-STARTPTS[a{i}]")
            if opt.audio_mode == "concat" or not use_xfade:
                fc.append("".join(f"[a{i}]" for i in range(n)) + f"concat=n={n}:v=0:a=1[ao]")
            else:
                last = "a0"
                for i in range(1, n):
                    fc.append(f"[{last}][a{i}]acrossfade=d={d:.3f}:c1=tri:c2=tri[ax{i}]")
                    last = f"ax{i}"
                fc.append(f"[{last}]atrim=0:{total:.3f}[ao]")
            aout = "ao"

    args += ["-filter_complex", ";".join(fc), "-map", f"[{vout}]"]
    if aout:
        args += ["-map", f"[{aout}]"]

    args += [
        "-c:v", "libx264", "-preset", str(opt.preset), "-crf", str(int(opt.crf)),
        "-pix_fmt", opt.pix_fmt if "10" in opt.pix_fmt else "yuv420p",
        "-movflags", "+faststart",
    ]
    if aout:
        args += ["-c:a", "aac", "-b:a", opt.audio_bitrate]
    args += ["-t", f"{total:.3f}", out_path]

    meta = {
        "fps": fps, "width": W, "height": H, "segments": n,
        "transition": opt.transition if use_xfade else "none",
        "transition_seconds": round(d, 3),
        "expected_duration": round(total, 3),
        "audio_mode": opt.audio_mode if aout else "none",
    }
    return args, meta


def run_ffmpeg(args: list[str], *, log_prefix: str = "[dm-stitch]") -> None:
    t0 = time.time()
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, encoding="utf-8", errors="replace", bufsize=1)
    tail: list[str] = []
    assert p.stdout is not None
    for line in p.stdout:
        line = line.rstrip()
        if not line:
            continue
        tail.append(line)
        if len(tail) > 40:
            tail.pop(0)
        if "frame=" in line or "Error" in line or "error" in line:
            print(f"{log_prefix} {line}", flush=True)
    rc = p.wait()
    if rc != 0:
        raise RuntimeError("ffmpeg 失败(rc=%s)\n%s" % (rc, "\n".join(tail[-15:])))
    print(f"{log_prefix} 完成，用时 {time.time() - t0:.1f}s", flush=True)


def output_dir() -> str:
    """优先写到 ComfyUI 的 output/video，方便在界面里直接看到。"""
    try:
        import folder_paths  # ComfyUI 内置
        base = folder_paths.get_output_directory()
    except Exception:
        base = os.path.join(os.getcwd(), "output")
    d = os.path.join(base, "video")
    os.makedirs(d, exist_ok=True)
    return d


def unique_path(dirpath: str, stem: str, ext: str = ".mp4") -> str:
    stem = re.sub(r"[^A-Za-z0-9_\-\u4e00-\u9fff]+", "_", stem or "DM_stitched").strip("_") or "DM_stitched"
    stamp = time.strftime("%Y%m%d_%H%M%S")
    p = os.path.join(dirpath, f"{stem}_{stamp}{ext}")
    k = 1
    while os.path.exists(p):
        p = os.path.join(dirpath, f"{stem}_{stamp}_{k}{ext}")
        k += 1
    return p


def stitch(paths: Iterable[str], *, out_path: str | None = None, output_name: str = "DM_stitched",
           options: StitchOptions | None = None,
           trims: Optional[list[tuple[float, float]]] = None) -> dict:
    """主入口：给定若干视频路径，拼成一条。返回 dict(files, meta, cmd)。

    overlap_mode="drop" 时（SCAIL2 式）：第 2..N 段**裁掉开头 overlap_frames 帧**，因为这段开头
    与上一段结尾是同一批帧（生成时用 `previous_frame_count` 续上的），不裁就会出现"回跳/重影"。
    """
    opt = options or StitchOptions()
    plist = [p for p in paths if p]
    if len(plist) < 1:
        raise ValueError("至少要 1 段视频")
    trims = list(trims or [(0.0, 0.0)] * len(plist))
    if len(trims) < len(plist):
        trims += [(0.0, 0.0)] * (len(plist) - len(trims))

    segs = [probe_segment(p, trims[i][0], trims[i][1]) for i, p in enumerate(plist)]

    # ---- SCAIL2 式：裁掉段首与上一段重复的帧 ----
    dropped = []
    if int(opt.overlap_frames) > 0 and opt.overlap_mode == "drop" and len(segs) > 1:
        fps_ref = opt.target_fps if opt.target_fps and opt.target_fps > 0 else (segs[0].fps or 24.0)
        ov_sec = float(opt.overlap_frames) / float(fps_ref)
        for i in range(1, len(segs)):
            s = segs[i]
            keep = max(s.eff_duration - ov_sec, 0.01)
            s.trim_start = s.trim_start + ov_sec
            s.trim_duration = keep
            dropped.append(round(ov_sec, 3))
        # 段间已连续 → 强制硬接（再叠淡化反而会糊/重影）
        opt.transition = "none"
        opt.transition_seconds = 0.0
        print(f"[dm-stitch] SCAIL2 式：每段裁掉开头 {opt.overlap_frames} 帧 = {ov_sec:.3f}s", flush=True)

    for s in segs:
        print(f"[dm-stitch] 段 {os.path.basename(s.path)}: {s.width}x{s.height} @{s.fps:.3f}fps "
              f"{s.eff_duration:.2f}s audio={'Y' if s.has_audio else 'N'}", flush=True)

    outp = out_path or unique_path(output_dir(), output_name)
    args, meta = build_command(segs, outp, opt)
    # 顺手估算"老做法（全部解码成 IMAGE 张量）"要多少内存，做对比
    fps_ref = meta["fps"]
    total_frames = sum(max(int(round(s.eff_duration * fps_ref)), 0) for s in segs)
    naive_gb = total_frames * meta["width"] * meta["height"] * 3 * 4 / (1024 ** 3)
    meta.update({"overlap_frames": int(opt.overlap_frames), "overlap_mode": opt.overlap_mode,
                 "dropped_head_seconds": dropped, "total_frames": total_frames,
                 "naive_image_batch_gb": round(naive_gb, 2),
                 "note": "本节点全程流式（ffmpeg），内存恒定，不把帧放进 IMAGE 张量"})
    print("[dm-stitch] ffmpeg " + " ".join(args[:6]) + " …", flush=True)
    run_ffmpeg(args)
    if not os.path.isfile(outp) or os.path.getsize(outp) == 0:
        raise RuntimeError(f"输出为空: {outp}")
    meta.update({"output": outp, "size_mb": round(os.path.getsize(outp) / 1048576, 2)})
    return {"files": [outp], "meta": meta, "cmd": args}


# --------------------------------------------------------------------------------------
# 命令行（方便脱离 ComfyUI 先验证）
# --------------------------------------------------------------------------------------
def _cli(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="DM 视频无缝拼接（流式，防 OOM）")
    ap.add_argument("videos", nargs="+", help="按顺序给 1~5 段视频")
    ap.add_argument("-o", "--out", default=None, help="输出文件路径")
    ap.add_argument("--transition", default="fade", choices=TRANSITIONS)
    ap.add_argument("--d", type=float, default=0.35, help="交叉淡化秒数")
    ap.add_argument("--fps", type=float, default=0.0, help="目标帧率(0=第一段)")
    ap.add_argument("--width", type=int, default=0)
    ap.add_argument("--height", type=int, default=0)
    ap.add_argument("--fit", default="crop", choices=FITS)
    ap.add_argument("--audio", default="crossfade", choices=AUDIO_MODES)
    ap.add_argument("--pix-fmt", default="yuv420p", choices=PIX_FMTS)
    ap.add_argument("--crf", type=int, default=18)
    ap.add_argument("--preset", default="fast")
    ap.add_argument("--quiet-meta", action="store_true")
    a = ap.parse_args(argv)
    opt = StitchOptions(transition=a.transition, transition_seconds=a.d, target_fps=a.fps,
                        width=a.width, height=a.height, fit=a.fit, audio_mode=a.audio,
                        pix_fmt=a.pix_fmt, crf=a.crf, preset=a.preset)
    r = stitch(a.videos, out_path=a.out, options=opt)
    if not a.quiet_meta:
        print(json.dumps(r["meta"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
