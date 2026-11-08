# -*- coding: utf-8 -*-
"""发布 dm-video-stitch 到 Comfy Registry —— 绕过 comfy-cli 硬编码的 30s 超时。

做法：复用 comfy-cli 自己的函数（validate_node_for_publishing / publish_node_version /
zip_files / upload_file_to_signed_url），只把 registry.api 里那个 DEFAULT_HTTP_TIMEOUT 调到 300s。

token 从 .registry-token 读，全程不打印。
"""
import os, sys, json, traceback

SP = r"F:\_tmp\comfycli\Lib\site-packages"
REPO = r"F:\dm-video-stitch-repo"
sys.path.insert(0, SP)
os.chdir(REPO)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

tok = open(os.path.join(REPO, ".registry-token"), encoding="utf-8").read().strip()
assert tok, "token 为空"
print(f"[1/4] token 已读取（长度 {len(tok)}，不回显）")

import comfy_cli.registry.api as ra
ra.DEFAULT_HTTP_TIMEOUT = 300.0          # ← 关键：原来是 30.0
print(f"[2/4] registry.api 超时已改为 {ra.DEFAULT_HTTP_TIMEOUT}s")

from comfy_cli.command.custom_nodes.command import validate_node_for_publishing, registry_api
from comfy_cli.file_utils import zip_files, upload_file_to_signed_url
from comfy_cli.constants import NODE_ZIP_FILENAME

cfg = validate_node_for_publishing()
name = cfg.project.name
ver = cfg.project.version
pub = cfg.tool_comfy.publisher_id
print(f"[3/4] 配置解析 OK: publisher={pub} node={name} version={ver}")

changelog = (
    "v1.0.0 首次发布：视频无缝拼接（流式防 OOM）。\n"
    "- 两个节点：DMVideoStitch5（5 段 VIDEO 直连）/ DMVideoStitchList（不限段数，路径列表）\n"
    "- 流式处理：100 段成片 200 秒仅 900 MB 内存（旧做法需 22 GB）\n"
    "- 支持 drop（裁掉段间重叠帧）/ crossfade（交叉淡化）两种衔接，自动归一化分辨率/fps/10bit\n"
    "- 音频 acrossfade、缺音轨自动补等长静音、支持整条 BGM 覆盖"
)

print("[4/4] 调用 API 发布（超时 300s）…")
try:
    resp = registry_api.publish_node_version(cfg, tok, changelog=changelog)
    print("    API OK，拿到 signedUrl")
    print("    节点版本:", getattr(resp.node_version, "version", "?"))
    zip_files(NODE_ZIP_FILENAME)
    size = os.path.getsize(NODE_ZIP_FILENAME)
    print(f"    打包完成 node.zip = {size} B")
    upload_file_to_signed_url(resp.signedUrl, NODE_ZIP_FILENAME)
    print("RESULT: PUBLISH OK")
except Exception as e:
    print("RESULT: PUBLISH FAILED ->", type(e).__name__, str(e)[:600])
    body = getattr(e, "body", None)
    if body:
        print("  body:", str(body)[:800])
    traceback.print_exc()
    sys.exit(1)
