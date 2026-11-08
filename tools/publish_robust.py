# -*- coding: utf-8 -*-
"""Comfy Registry 发布（强健版）：API 长超时 + 签名 URL 上传多轮重试（直连/代理交替）

- token 从 .registry-token 读，不打印
- 上传目标 storage.googleapis.com 在本机抖动严重（10 次只成 1 次），所以：12 轮 × 2 通道，单次 120s
- 403/过期 → 重新拿一次 signedUrl；版本冲突(409) → 自动把 patch 版号 +1 再发
"""
import os, sys, json, re, time, traceback

SP = r"F:\_tmp\comfycli\Lib\site-packages"
REPO = r"F:\dm-video-stitch-repo"
PROXY = "http://127.0.0.1:51081"
sys.path.insert(0, SP)
os.chdir(REPO)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

tok = open(os.path.join(REPO, ".registry-token"), encoding="utf-8").read().strip()
assert tok, "token 为空"
print(f"[i] token 读取 OK（长度 {len(tok)}）")

import comfy_cli.registry.api as ra
ra.DEFAULT_HTTP_TIMEOUT = 300.0
from comfy_cli.command.custom_nodes.command import validate_node_for_publishing, registry_api
from comfy_cli.file_utils import zip_files
from comfy_cli.constants import NODE_ZIP_FILENAME

import requests

CHANGELOG = (
    "v1.0.0 首次发布：视频无缝拼接（流式防 OOM）。\n"
    "- 两个节点：DMVideoStitch5（5 段 VIDEO 直连）/ DMVideoStitchList（不限段数，路径列表）\n"
    "- 流式处理：100 段成片 200 秒仅约 900 MB 内存（旧做法需 22 GB）\n"
    "- drop（裁掉段间重叠帧）/ crossfade（交叉淡化）两种衔接；自动归一化分辨率/fps/10bit\n"
    "- 音频 acrossfade、缺音轨自动补等长静音、支持整条 BGM 覆盖"
)


def bump_version():
    p = os.path.join(REPO, "pyproject.toml")
    t = open(p, encoding="utf-8").read()
    m = re.search(r'^version\s*=\s*"(\d+)\.(\d+)\.(\d+)"', t, re.M)
    if not m:
        raise SystemExit("pyproject.toml 里找不到 version")
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    new = f"{major}.{minor}.{patch + 1}"
    t = re.sub(r'^version\s*=\s*"\d+\.\d+\.\d+"', f'version = "{new}"', t, count=1, flags=re.M)
    open(p, "w", encoding="utf-8", newline="\n").write(t)
    print(f"    版本号 {major}.{minor}.{patch} → {new}")
    return new


def publish_once():
    cfg = validate_node_for_publishing()
    print(f"    publisher={cfg.tool_comfy.publisher_id} node={cfg.project.name} version={cfg.project.version}")
    resp = registry_api.publish_node_version(cfg, tok, changelog=CHANGELOG)
    return resp


def upload(signed_url, path):
    size = os.path.getsize(path)
    print(f"[i] 上传 node.zip = {size} B（12 轮 × 直连/代理，单次 120s）")
    chans = [(None, "直连"), ({"https": PROXY, "http": PROXY}, "代理")]
    for rnd in range(1, 13):
        for proxies, label in chans:
            try:
                with open(path, "rb") as f:
                    t0 = time.time()
                    r = requests.put(signed_url, data=f, headers={"Content-Type": "application/zip"},
                                     timeout=(15, 120), proxies=proxies)
                dt = time.time() - t0
                print(f"    第{rnd}轮 {label}: HTTP {r.status_code}  {dt:.1f}s")
                if r.status_code == 200:
                    return True, "ok"
                if r.status_code in (403, 400) and "expired" in (r.text or "").lower():
                    return False, "expired"
            except Exception as e:
                print(f"    第{rnd}轮 {label}: {type(e).__name__} {str(e)[:90]}")
            time.sleep(1.0)
    return False, "all-attempts-failed"


def main():
    for attempt in range(3):
        print(f"=== 尝试 {attempt + 1}/3：申请签名 URL ===")
        try:
            resp = publish_once()
        except Exception as e:
            msg = str(e)
            print("    发布 API 失败:", type(e).__name__, msg[:300])
            if "already" in msg.lower() or "exists" in msg.lower() or "conflict" in msg.lower() or getattr(e, "status", 0) == 409:
                bump_version()
                continue
            time.sleep(3)
            continue

        os.write(1, b"")  # noop
        open(os.path.join(REPO, ".signed-url.txt"), "w", encoding="utf-8").write(resp.signedUrl)
        print("    拿到 signedUrl（已存 .signed-url.txt）")
        zip_files(NODE_ZIP_FILENAME)
        ok, why = upload(resp.signedUrl, NODE_ZIP_FILENAME)
        if ok:
            print("RESULT: PUBLISH OK")
            print("  https://registry.comfy.org/nodes/dm-video-stitch")
            return 0
        if why == "expired":
            print("    签名过期，重新申请…")
            continue
        print("    上传全部失败（网络抖动），再试一轮 API…")
    print("RESULT: FAILED（需要换网络/开全局代理再试）")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(2)
