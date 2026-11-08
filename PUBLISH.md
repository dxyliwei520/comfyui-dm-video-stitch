# 发布到 GitHub + 接入 ComfyUI-Manager

> 本地这个目录（`F:\dm-video-stitch-repo`）**已经是一个完整的 git 仓库**（commit `dbae7fd`），
> remote 已指向 `git@github.com:dxyliwei520/comfyui-dm-video-stitch.git`。
> **只差你在 GitHub 上建一个空仓库**（30 秒），然后跑 `publish.ps1` 就上线。

---

## 第 1 步：在 GitHub 建空仓库（你来，30 秒）

1. 打开 <https://github.com/new>
2. **Repository name**：`comfyui-dm-video-stitch`
3. 可见性选 **Public**（⚠️ Manager 一键安装/Git URL 需要公开；`dm-factory` 是私有的，不适合）
4. **不要**勾 "Add a README / .gitignore / license"（保持空仓库，否则第一次 push 会冲突）
5. Create

## 第 2 步：推上去（一条命令，我来跑也行）

```powershell
cd F:\dm-video-stitch-repo
git push -u origin main
```

或双击本目录的 **`publish.ps1`**（会做 push + 打印后面所有步骤）。

---

## 第 3 步：接入 ComfyUI-Manager（三种方式，按省事程度排）

### 方式 A：直接 clone（最稳，不依赖 Manager）

```powershell
cd D:\ComfyUI_experimental\ComfyUI_windows_portable\ComfyUI\custom_nodes
git clone https://github.com/dxyliwei520/comfyui-dm-video-stitch dm_video_stitch
```
然后重启 ComfyUI + 刷新浏览器。（本机 D 盘**已经装好了**，这一步只是给别人/重装用）

### 方式 B：Manager → Install via Git URL

1. 本机 Manager 的 `config.ini` 里 `allow_git_url_install` 目前是 **False**（2026-11-08 已改成 True，见下）
2. Manager → `Install Custom Nodes` → 顶部 **Install via Git URL** → 粘贴：
   `https://github.com/dxyliwei520/comfyui-dm-video-stitch`
3. 重启 ComfyUI

### 方式 C：发布到官方 Registry（**这是"所有人在 Manager 里搜得到"的唯一办法**）

需要你的 registry 账号（我无法代办），两条命令：

```powershell
pip install comfy-cli
comfy --skip-prompt model registry login       # 浏览器登录 GitHub
# 先把 pyproject.toml 里的 PublisherId 填成登录后显示的 ID
comfy node publish                              # 在 F:\dm-video-stitch-repo 里执行
```

发布后，任何人的 Manager 里都能搜到 **DM Video Stitch**，也能 `comfy node install dm-video-stitch`。

> ⚠️ 官方 Registry 是**公开**的：`pyproject.toml` 里的 PublisherId 必填，且不能含私有信息。

---

## 文件说明

| 文件 | 用途 |
|---|---|
| `__init__.py` / `core.py` | 两个节点（×5 版 / 不限段数版）+ 拼接核心 |
| `README.md` | 使用方法、优点、压测数据、FAQ（GitHub 首页展示的就是它） |
| `examples/example_workflow.json` | 示例工作流，拖进 ComfyUI 即可用 |
| `pyproject.toml` | Comfy **Registry** 元数据（发布前填 PublisherId） |
| `custom-node-list.json` | ComfyUI-Manager 自定义节点清单格式（供渠道/离线索引） |
| `requirements.txt` / `LICENSE` | 无第三方依赖 / MIT |

## 版本与更新

改完代码后的发布流程：

```powershell
# 1) 同步到本目录（从 ComfyUI 的 custom_nodes 复制的）
copy D:\ComfyUI_experimental\...\custom_nodes\dm_video_stitch\*.py .
# 2) 改 pyproject.toml 的 version
# 3) 提交并推送
git add -A; git commit -m "feat: ..."; git push
```
