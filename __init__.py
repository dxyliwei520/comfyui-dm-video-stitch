# -*- coding: utf-8 -*-
"""
dm_video_stitch —— ComfyUI 节点：多段视频「无痕」拼接（流式，防 OOM）

为什么要这个节点：
    多模态/动作迁移类工作流常见的收尾是
        GetVideoComponents × N → BatchImagesNode → CreateVideo → SaveVideo
    这一步会把 N 段**所有帧**解成 float32 IMAGE 张量堆在内存/显存里。
    实测：5 段 480x864、共 100 秒 ≈ 12 GB；960x1728@96fps 的 5 段 ≈ 25 GB+ → 必 OOM。

本节点改成「只拿文件路径，交给 ffmpeg 流式拼」：
    每段的帧进内存的是**几帧**，不是全部 → 内存恒定，段数再多也不涨。
    同时做了两件让接缝看不见的事：
      1) 段间衔接有三种模式（见 stitch_mode）：drop（SCAIL2 式，裁掉重叠帧后硬接）/ crossfade / none
      2) 每段先归一化分辨率、fps、pix_fmt、SAR（素材规格不一致也能拼）

作者：DSH（2026-11-08）
"""

from __future__ import annotations

import os
import tempfile
import wave

import torch

from .core import (AUDIO_MODES, FITS, OVERLAP_MODES, PIX_FMTS, TRANSITIONS,
                   StitchOptions, stitch as do_stitch, find_ffmpeg)

# 返回 VIDEO 类型（ComfyUI 0.42+）
try:
    from comfy_api.latest._input_impl.video_types import VideoFromFile
except Exception:  # 兼容旧布局
    try:
        from comfy_api.input_impl import VideoFromFile  # type: ignore
    except Exception:  # pragma: no cover
        VideoFromFile = None  # type: ignore


def _save_audio_tensor(audio: dict, path: str) -> str:
    """把 ComfyUI 的 AUDIO(dict: waveform/float32, sample_rate) 写成 16-bit wav。
    AUDIO 结构：{"waveform": [B, C, T] float, "sample_rate": int}"""
    wf = audio.get("waveform")
    sr = int(audio.get("sample_rate") or 48000)
    if wf is None:
        raise ValueError("AUDIO 里没有 waveform")
    if isinstance(wf, torch.Tensor):
        wf = wf.detach().cpu().float()
    if wf.dim() == 3:          # [B, C, T] → 取第 0 个 batch
        wf = wf[0]
    if wf.dim() == 1:
        wf = wf.unsqueeze(0)
    ch = wf.shape[0]
    data = wf.transpose(0, 1).contiguous().clamp(-1.0, 1.0).numpy()  # [T, C]
    pcm = (data * 32767.0).astype("<i2").tobytes()
    with wave.open(path, "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)
    return path


def _path_of(video) -> str | None:
    """从 ComfyUI 的 VIDEO 对象里拿**源文件路径**（关键：不触发解码）。"""
    if video is None:
        return None
    if isinstance(video, str):
        return video if os.path.isfile(video) else None
    # 0.42 的 VideoFromFile / VideoFromList 都实现了 get_stream_source()
    getter = getattr(video, "get_stream_source", None)
    if callable(getter):
        try:
            src = getter()
        except Exception:
            src = None
        if isinstance(src, str) and os.path.isfile(src):
            return src
        if hasattr(src, "read"):
            # BytesIO / 流：落一个临时文件（极少数情况）
            import io as _io
            fd, tmp = tempfile.mkstemp(suffix=".mp4")
            os.close(fd)
            try:
                src.seek(0)
            except Exception:
                pass
            with open(tmp, "wb") as f:
                f.write(src.read())
            return tmp
    # 兜底：老式 VIDEO 可能是 (images, audio, fps) 元组 → 无损取出会 OOM，直接拒绝并给出明确提示
    raise RuntimeError(
        "这个 VIDEO 输入不是「文件型」，拿不到源文件（可能是内存里的帧张量）。\n"
        "请先把它 SaveVideo 落盘，再用 LoadVideo 读进来接本节点 —— 这样才不会 OOM。"
    )


def _trim_of(video) -> tuple[float, float]:
    """若 VIDEO 带裁剪窗口（VideoSlice/VideoTrim 产生的），把 (start, duration) 取出来。"""
    getter = getattr(video, "get_active_trim_window", None)
    if callable(getter):
        try:
            s, d = getter()
            return float(s or 0.0), float(d or 0.0)
        except Exception:
            pass
    return 0.0, 0.0


class DMVideoStitch5:
    """把最多 5 段视频流式拼成 1 条（防 OOM + 无痕衔接）。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "video_1": ("VIDEO", {"tooltip": "第 1 段（必填）"}),
                "stitch_mode": (OVERLAP_MODES, {"default": "drop",
                    "tooltip": "drop = SCAIL2 式：段间本来共享 previous_frame_count 帧，裁掉重复帧再硬接（画面连续、无重影）；"
                               "crossfade = 段间无关联时用交叉淡化掩盖；none = 纯硬接"}),
                "overlap_frames": ("INT", {"default": 5, "min": 0, "max": 120, "step": 1,
                    "tooltip": "段间重复帧数（对应 WanSCAILToVideo 的 previous_frame_count，默认 5）。"
                               "stitch_mode=drop 时，第 2 段起会裁掉开头这么多帧"}),
                "transition_seconds": ("FLOAT", {"default": 0.35, "min": 0.0, "max": 3.0, "step": 0.05,
                    "tooltip": "仅 stitch_mode=crossfade 时生效的交叉淡化时长"}),
                "target_fps": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 120.0, "step": 1.0,
                    "tooltip": "0 = 用第 1 段的帧率；素材 fps 不一致时这里统一"}),
                "width": ("INT", {"default": 0, "min": 0, "max": 8192, "step": 2,
                    "tooltip": "0 = 用第 1 段宽度"}),
                "height": ("INT", {"default": 0, "min": 0, "max": 8192, "step": 2,
                    "tooltip": "0 = 用第 1 段高度"}),
                "fit": (FITS, {"default": "crop", "tooltip": "crop=裁切填满不变形 / pad=加黑边 / stretch=拉伸"}),
                "pix_fmt": (PIX_FMTS, {"default": "yuv420p", "tooltip": "10-bit 素材要保留位深选 yuv420p10le"}),
                "audio_mode": (AUDIO_MODES, {"default": "crossfade",
                    "tooltip": "crossfade=段间音频交叉淡化(推荐) / concat=直接首尾接 / first_only=只留第 1 段并补齐 / silent / none"}),
                "crf": ("INT", {"default": 18, "min": 0, "max": 35, "step": 1}),
                "preset": (["veryfast", "fast", "medium", "slow"], {"default": "fast"}),
                "output_name": ("STRING", {"default": "DM_stitched"}),
            },
            "optional": {
                "video_2": ("VIDEO", {"tooltip": "第 2 段（不接则跳过）"}),
                "video_3": ("VIDEO", {}),
                "video_4": ("VIDEO", {}),
                "video_5": ("VIDEO", {}),
                "audio_override": ("AUDIO", {"tooltip": "用一条完整音轨覆盖所有段（多模态里常这么干）"}),
            },
        }

    RETURN_TYPES = ("VIDEO", "STRING")
    RETURN_NAMES = ("video", "path")
    FUNCTION = "run"
    CATEGORY = "DM/Video"
    DESCRIPTION = "多段视频无痕拼接（流式，防 OOM）；支持 SCAIL2 式重叠帧裁切与交叉淡化两种衔接。"

    def run(self, video_1, stitch_mode, overlap_frames, transition_seconds, target_fps,
            width, height, fit, pix_fmt, audio_mode, crf, preset, output_name,
            video_2=None, video_3=None, video_4=None, video_5=None, audio_override=None):

        find_ffmpeg()  # 早期报错更清楚
        vids = [video_1, video_2, video_3, video_4, video_5]
        paths, trims = [], []
        for v in vids:
            if v is None:
                continue
            p = _path_of(v)
            if not p:
                continue
            paths.append(p)
            trims.append(_trim_of(v))
        if not paths:
            raise RuntimeError("没有可用的视频输入（至少要接 video_1）")

        opt = StitchOptions(
            transition=("fade" if stitch_mode == "crossfade" else "none"),
            transition_seconds=float(transition_seconds),
            target_fps=float(target_fps or 0.0),
            width=int(width or 0), height=int(height or 0), fit=fit,
            pix_fmt=pix_fmt, audio_mode=audio_mode, crf=int(crf), preset=preset,
            overlap_frames=int(overlap_frames or 0), overlap_mode=("drop" if stitch_mode == "drop" else "crossfade"),
        )
        tmp_audio = None
        if audio_override is not None:
            fd, tmp_audio = tempfile.mkstemp(suffix=".wav")
            os.close(fd)
            _save_audio_tensor(audio_override, tmp_audio)
            opt.audio_override = tmp_audio

        try:
            res = do_stitch(paths, output_name=output_name, options=opt, trims=trims)
        finally:
            if tmp_audio and os.path.isfile(tmp_audio):
                try:
                    os.remove(tmp_audio)
                except Exception:
                    pass

        out = res["files"][0]
        meta = res["meta"]
        print("[dm-stitch] meta:", meta, flush=True)
        if VideoFromFile is None:
            raise RuntimeError("当前 ComfyUI 版本没有 InputImpl.VideoFromFile，无法返回 VIDEO；请改用 'path' 输出接 SaveVideo")
        return (VideoFromFile(out), out)


def _resolve_path(s: str) -> str:
    """把用户写的路径解析成真实文件：绝对路径 > ComfyUI input 目录 > ComfyUI output 目录。"""
    s = (s or "").strip().strip('"').strip("'")
    if not s:
        return ""
    if os.path.isfile(s):
        return s
    cands = []
    try:
        import folder_paths
        for d in (folder_paths.get_input_directory(), folder_paths.get_output_directory()):
            cands.append(os.path.join(d, s))
            cands.append(os.path.join(d, "video", s))
    except Exception:
        pass
    for c in cands:
        if os.path.isfile(c):
            return c
    return ""


# 与 DMVideoStitch5 共用的参数定义（列表版复用，避免两处不一致）
def _common_inputs():
    return {
        "stitch_mode": (OVERLAP_MODES, {"default": "drop",
            "tooltip": "drop = 段间本来共享 previous_frame_count 帧 → 裁掉重复帧再硬接（画面连续、无重影）；"
                       "crossfade = 段间各自独立生成时用交叉淡化掩盖；none = 纯硬接"}),
        "overlap_frames": ("INT", {"default": 5, "min": 0, "max": 200, "step": 1,
            "tooltip": "段间重复帧数（要和生成侧 previous_frame_count 一致；各段独立生成就填 0）"}),
        "transition_seconds": ("FLOAT", {"default": 0.35, "min": 0.0, "max": 3.0, "step": 0.05,
            "tooltip": "仅 stitch_mode=crossfade 生效"}),
        "target_fps": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 120.0, "step": 1.0,
            "tooltip": "0 = 用第 1 段帧率；素材 fps 不一致时统一"}),
        "width": ("INT", {"default": 0, "min": 0, "max": 8192, "step": 2, "tooltip": "0 = 用第 1 段宽度"}),
        "height": ("INT", {"default": 0, "min": 0, "max": 8192, "step": 2, "tooltip": "0 = 用第 1 段高度"}),
        "fit": (FITS, {"default": "crop", "tooltip": "crop=裁切填满不变形 / pad=加黑边 / stretch=拉伸"}),
        "pix_fmt": (PIX_FMTS, {"default": "yuv420p", "tooltip": "10-bit 素材要保留位深选 yuv420p10le"}),
        "audio_mode": (AUDIO_MODES, {"default": "crossfade",
            "tooltip": "crossfade=段间音频交叉淡化(推荐) / concat=首尾直连 / first_only=只留第1段补齐 / silent / none"}),
        "crf": ("INT", {"default": 18, "min": 0, "max": 35, "step": 1}),
        "preset": (["veryfast", "fast", "medium", "slow"], {"default": "fast"}),
        "output_name": ("STRING", {"default": "DM_stitched"}),
    }


class DMVideoStitchList:
    """一次拼 **N 段（不限数量）**：在文本框里一行一个文件路径。

    为什么单独做一个：单节点固定 5 个 VIDEO 输入，超过 5 段只能串联（每串一次就重编码一次，画质会掉）。
    列表版**一次 ffmpeg 只编码一遍**，10 段 20 段都一样，内存仍是恒定（流式）。
    """

    @classmethod
    def INPUT_TYPES(cls):
        d = {
            "required": {
                "paths": ("STRING", {"multiline": True, "default": "",
                    "tooltip": "一行一个视频路径（绝对路径，或相对 ComfyUI 的 input/output 目录，如 video/xxx.mp4）。顺序 = 拼接顺序。\n"
                               "可以随便多少段；空行会忽略。"}),
            },
            "optional": {
                "audio_override": ("AUDIO", {"tooltip": "用一条完整音轨覆盖所有段（会补静音到视频总长）"}),
            },
        }
        d["required"].update(_common_inputs())
        return d

    RETURN_TYPES = ("VIDEO", "STRING")
    RETURN_NAMES = ("video", "path")
    FUNCTION = "run"
    CATEGORY = "DM/Video"
    DESCRIPTION = "把任意段数视频（一行一个路径）流式拼成一条；同样支持 drop/crossfade 与音频处理。"

    def run(self, paths, stitch_mode, overlap_frames, transition_seconds, target_fps,
            width, height, fit, pix_fmt, audio_mode, crf, preset, output_name,
            audio_override=None):
        find_ffmpeg()
        raw = [ln.strip() for ln in (paths or "").replace("\r", "\n").split("\n")]
        resolved, missing = [], []
        for ln in raw:
            if not ln or ln.startswith("#"):
                continue
            p = _resolve_path(ln)
            (resolved if p else missing).append(p or ln)
        if missing:
            raise RuntimeError("这些路径找不到（要么写绝对路径，要么放在 ComfyUI 的 input/output 目录里）：\n  "
                               + "\n  ".join(missing[:10]))
        if not resolved:
            raise RuntimeError("paths 里没有有效路径")

        opt = StitchOptions(
            transition=("fade" if stitch_mode == "crossfade" else "none"),
            transition_seconds=float(transition_seconds),
            target_fps=float(target_fps or 0.0),
            width=int(width or 0), height=int(height or 0), fit=fit,
            pix_fmt=pix_fmt, audio_mode=audio_mode, crf=int(crf), preset=preset,
            overlap_frames=int(overlap_frames or 0), overlap_mode=("drop" if stitch_mode == "drop" else "crossfade"),
        )
        tmp_audio = None
        if audio_override is not None:
            fd, tmp_audio = tempfile.mkstemp(suffix=".wav")
            os.close(fd)
            _save_audio_tensor(audio_override, tmp_audio)
            opt.audio_override = tmp_audio
        try:
            res = do_stitch(resolved, output_name=output_name, options=opt)
        finally:
            if tmp_audio and os.path.isfile(tmp_audio):
                try:
                    os.remove(tmp_audio)
                except Exception:
                    pass
        out = res["files"][0]
        print("[dm-stitch][list] 段数=%d meta=%s" % (len(resolved), res["meta"]), flush=True)
        if VideoFromFile is None:
            raise RuntimeError("当前 ComfyUI 没有 InputImpl.VideoFromFile，请改用 'path' 输出接 SaveVideo")
        return (VideoFromFile(out), out)


NODE_CLASS_MAPPINGS = {"DMVideoStitch5": DMVideoStitch5, "DMVideoStitchList": DMVideoStitchList}
NODE_DISPLAY_NAME_MAPPINGS = {
    "DMVideoStitch5": "DM 视频无缝拼接 ×5（防OOM）",
    "DMVideoStitchList": "DM 视频无缝拼接 · 不限段数（路径列表）",
}
