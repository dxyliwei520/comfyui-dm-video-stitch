# DM Video Stitch · 视频无缝拼接（防 OOM）

> ComfyUI 自定义节点：把**多段视频（含音频）**拼成一条，**接缝不可见**、**内存恒定**、**段数几乎不限**。
> A ComfyUI custom node that stitches multiple video segments (with audio) into one — seamless seams,
> constant memory, effectively unlimited segment count.

[![ComfyUI](https://img.shields.io/badge/ComfyUI-0.42%2B-blue)](https://github.com/comfyanonymous/ComfyUI)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

提供两个节点 / Two nodes:

| 节点 | 用途 |
|---|---|
| **DM 视频无缝拼接 ×5（防OOM）** | 工作流内直接用，最多接 5 段 **VIDEO**（`video_1..video_5`），路径全自动 |
| **DM 视频无缝拼接 · 不限段数（路径列表）** | 拼**任意段数**的已有文件：`paths` 一行一个路径，**一次编码拼完** |

---

## 一、它解决什么问题（Why）

多模态 / 动作迁移类工作流的常见收尾是：

```
GetVideoComponents × N → BatchImagesNode → CreateVideo → SaveVideo
```

`BatchImagesNode` 会把 N 段的**全部帧**解码成 float32 IMAGE 张量并合并，于是内存随成片时长**线性爆炸**：

| 输入 | 老做法内存 |
|---|---|
| 5 段 × 20 秒 @24fps，480×864 | **≈ 12 GB** |
| 5 段 @96fps，960×1728 | **≈ 40 GB** |
| 100 段 × 2 秒，480×864 | **≈ 22 GB（必 OOM）** |

本节点**只取源文件路径**（`VIDEO.get_stream_source()`），交给 ffmpeg 流式处理 —— 内存里永远只有几帧。

---

## 二、优点（Advantages，全部实测）

| # | 优点 | 实测 |
|---|---|---|
| 1 | **不 OOM** | 100 段成片 3′20″ → 峰值 **900 MB**；同输入老做法 **22.25 GB** |
| 2 | **快** | 5 段 70 秒 → **2.4 s**；100 段 → **3.6 s**（全片只编码一遍） |
| 3 | **段数几乎不限** | 内存只随段数线性涨：硬接 **≈9 MB/段**、淡化 **≈24 MB/段**（与成片时长无关） |
| 4 | **接缝真的处理过** | `drop`：裁掉段间**重叠帧**（链式生成时每段开头重复 N 帧，不裁就"回跳"）<br>`crossfade`：给**独立生成**的段做淡入淡出 |
| 5 | **规格不一致也能拼** | 每段先归一化 分辨率 / fps / pix_fmt（含 10bit）/ SAR |
| 6 | **音频更靠谱** | 段间 `acrossfade`、**缺音轨自动补等长静音**、支持整条 BGM 覆盖（自动补静音到片长） |

### 压测数据（480×864@24、每段 2 秒、带音轨）

| 模式 | 段数 | 输出时长 | 用时 | 峰值内存 | 老做法需 |
|---|---|---|---|---|---|
| crossfade 0.3s | 5 | 8.8 s | 0.2 s | **245 MB** | 1.11 GB |
| crossfade 0.3s | 20 | 34.3 s | 0.8 s | **638 MB** | 4.45 GB |
| crossfade 0.3s | 40 | 68.3 s | 1.6 s | **967 MB** | 8.90 GB |
| 硬接 | 40 | 80.0 s | 1.5 s | **462 MB** | 8.90 GB |
| **硬接** | **100** | **200.0 s** | **3.6 s** | **900 MB** | **22.25 GB** |

### 与内置 `ConcatenateVideo` 的区别

内置节点确实"不解码直接拼"，但它：**要求所有段规格完全一致**、**只做硬切**、**不处理段间重叠帧**、**不做音频交叉淡化**。
本节点把这几件事都做了，所以混规格素材、链式生成的段落也能直接拼。

---

## 三、安装（Install）

### 方式 A：git clone（推荐）

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/dxyliwei520/comfyui-dm-video-stitch dm_video_stitch
```
重启 ComfyUI（**刷新浏览器**才能看到新节点）。

### 方式 B：ComfyUI-Manager

- Manager → `Install Custom Nodes` → **Install via Git URL** → 粘贴上面的仓库地址
- 或把本仓库的 `custom-node-list.json` raw 地址加进 Manager 的自定义节点列表渠道

### 方式 C：手动

把 `dm_video_stitch/` 整个目录放进 `ComfyUI/custom_nodes/` 即可。**没有第三方 pip 依赖**
（只用到 `av`（PyAV），ComfyUI 本体已自带）。

### 前置条件

- ComfyUI **0.42+**（`VIDEO` 类型与 `VideoFromFile.get_stream_source()` 需要此版本）
- 本机有 **ffmpeg.exe**（找不到时设环境变量 `DM_FFMPEG=<ffmpeg 绝对路径>`）

---

## 四、使用方法（Usage）

### 4.1 节点 A：`DM 视频无缝拼接 ×5（防OOM）`

```
每段的 VIDEO ──┬──▶ DM 视频无缝拼接 ×5 ──▶ video ──▶ SaveVideo
               │        （video_1 … video_5）
```

- 5 段 `VIDEO` 直接接 `video_1..video_5`（不接的口留空即可）
- 输出 `video` 接 `SaveVideo`；`path` 是文件路径字符串
- **参数关键：`overlap_frames` 必须等于生成侧的重叠帧数**
  （如 `WanSCAILToVideo.previous_frame_count`；各段独立生成则填 `0` 并改用 `crossfade`）

### 4.2 节点 B：`DM 视频无缝拼接 · 不限段数（路径列表）`

`paths` 一行一个路径，按**书写顺序**拼接：

```
video/seg_00018.mp4
video/seg_00019.mp4
video/seg_00020.mp4
D:\footage\shot4.mp4
```

三种写法都支持（实测）：

| 写法 | 例子 |
|---|---|
| 相对 ComfyUI `output` 目录（**推荐**） | `video/xxx.mp4` |
| 只写文件名（自动在 `input/`、`output/`、`output/video/` 里找） | `xxx.mp4` |
| 绝对路径 | `D:\footage\xxx.mp4` |

空行忽略；`#` 开头为注释；找不到时**会明确报错并列出缺失项**。

### 4.3 `audio_override` 是什么

可选 **AUDIO** 输入：

- **接了** → 全片用这条音轨，**丢弃各段自带声音**；比画面短会**自动补静音**到片长
- **不接** → 用各段自己的音频，段间按 `audio_mode` 处理

典型用法：5 段画面 + 一条完整 BGM/配音 → 画面走 `video_*`，BGM 接 `audio_override`。

### 4.4 参数表

| 参数 | 默认 | 说明 |
|---|---|---|
| `stitch_mode` | `drop` | `drop`=裁掉段间重叠帧后硬接（链式生成首选）/ `crossfade`=淡化（独立生成）/ `none`=纯硬接 |
| `overlap_frames` | `5` | 段间重复帧数（= 生成侧 `previous_frame_count`；独立生成填 0） |
| `transition_seconds` | `0.35` | 仅 `crossfade` 生效 |
| `target_fps` | `0` | 0=用第 1 段；素材 fps 不一致时统一 |
| `width` / `height` | `0` | 0=用第 1 段 |
| `fit` | `crop` | `crop` 裁切填满（不变形）/ `pad` 加黑边 / `stretch` 拉伸 |
| `pix_fmt` | `yuv420p` | 要保 10-bit 选 `yuv420p10le` |
| `audio_mode` | `crossfade` | `crossfade` / `concat` / `first_only` / `silent` / `none` |
| `crf` / `preset` | `18` / `fast` | 画质/速度；在意二次压缩可 `crf 14~16` |
| `output_name` | `DM_stitched` | 输出到 `output/video/DM_stitched_<时间戳>.mp4` |

---

## 五、示例工作流

`example_workflow.json`（5×LoadVideo → 拼接 → SaveVideo）：ComfyUI 里**拖进画布**即可。
把 `LoadVideo` 的 `file` 换成你自己的 5 段，然后运行。

---

## 六、常见问题（FAQ）

| 现象 | 处理 |
|---|---|
| `这个 VIDEO 输入不是「文件型」…` | 上游是内存里的帧（如 `CreateVideo`）。先 `SaveVideo` 落盘再 `LoadVideo`，或直接接各段的 VIDEO 输出 |
| `找不到 ffmpeg.exe` | 设环境变量 `DM_FFMPEG` 指向 ffmpeg 绝对路径 |
| 拼接处"回跳/重复" | `overlap_frames` 与生成侧不一致（填大了丢帧、填小了重复） |
| 拼接处"闪一下" | 段间没有共享帧 → 改 `stitch_mode=crossfade`，`transition_seconds=0.3~0.4` |
| 画面被裁掉 | `fit` 改 `pad`（黑边）或 `stretch` |
| 声音断一下 | 保持 `audio_mode=crossfade`；某段没音轨会自动补静音 |
| 装了但节点搜不到 | **刷新浏览器**（Ctrl+F5）；ComfyUI 的节点表只在页面加载时拉取 |

---

## 七、限制（Honest limits）

1. 会**重编码一次**（libx264）；对二次压缩敏感请把 `crf` 调低。
2. 音频统一转 **AAC 192k**（原音轨是高码率 wav 会有损）。
3. 它**修不了"内容不连续"** —— 那必须由生成侧的 `previous_frames` / 首帧条件决定；节点只负责裁重叠帧或淡化。
4. 10-bit 时色彩按 BT.709 默认处理，**HDR 元数据不保证完整保留**。

---

## 八、许可 / License

MIT（见 `LICENSE`）。作者：**dxyliwei520**（DSH）
