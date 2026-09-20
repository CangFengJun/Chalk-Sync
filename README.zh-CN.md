# ChalkSync 中文使用说明

让课程变成知道自己出处的笔记。

ChalkSync 用于把课程视频和带时间戳的 Markdown 字幕加工成可追溯的课程笔记。

## 项目缘起

ChalkSync 最初是为了用 Vibe Learning 的方式学习蒋炎岩老师讲授的南京大学《生成式软件工程》课程而开发的。这门课的知识不仅来自讲述，也分布在持续变化的板书和投影/PPT 中，单纯总结字幕会丢失重要上下文。因此，仓库预先配置了 2026 年课程目录，并把板书/PPT 区域识别、视觉证据提取和可跳转的时间戳链接作为核心能力。

处理流程本身并不局限于这门课：只要提供本地视频和带时间戳的 Markdown 字幕，也可以用于其他课程。

它会完成以下工作：

- 解析字幕时间轴；
- 从视频中抽取代表性画面；
- 识别板书区和投影/PPT/屏幕区；
- 分别提取板书与投影画面的变化帧；
- 用工作模型分段整理字幕和画面信息；
- 用最终模型对照原始字幕核验并生成完整笔记；
- 生成一个视频与笔记并排显示的本地播放器，点击笔记时间戳即可跳转。

模型完全由用户指定，仓库不会自动选择或替换模型。

## 一、准备环境

需要：

- Python 3.9 或更高版本；
- `ffmpeg` 和 `ffprobe`；
- 可调用所选模型的 API key；
- 如果使用兼容接口，可通过 `OPENAI_BASE_URL` 指定地址。

macOS 可以安装媒体工具：

```bash
brew install ffmpeg
```

检查当前环境：

```bash
./scripts/chalksync doctor
```

正常情况下会显示：

```text
python           OK
ffmpeg           OK
ffprobe          OK
OPENAI_API_KEY   OK
```

不要把 API key 写进仓库、课程配置或命令示例。请通过系统环境、密钥管理器或受保护的 shell 配置提供。

## 二、创建课程目录

每门课程使用一个独立目录：

```bash
./scripts/chalksync init courses/prompt-engineering \
  --title "提示词工程 [02-Raw/26生成式软件工程/NJU]"
```

本仓库已经为 01–05 五门正式课程创建好目录，并排除 8 月 28 日发布的短片：

```text
courses/
├── welcome-to-the-future/inbox/
├── prompt-engineering/inbox/
├── repository-management/inbox/
├── repository-management-2/inbox/
└── software-engineering-origins/inbox/
```

完整映射见 [`courses/README.md`](courses/README.md)。

## 三、上传视频和字幕

在课程的 `inbox/` 中放入且只放入：

- 一份视频；
- 一份 `.md` 字幕文件。

文件名可以自行决定。例如：

```text
courses/prompt-engineering/inbox/
├── lecture.mp4
└── subtitles.md
```

支持的视频扩展名：

```text
.mp4  .mkv  .webm  .mov  .m4v
```

### 字幕格式

每行必须以 `MM:SS` 或 `HH:MM:SS` 开头，时间戳后可以直接连接正文，不要求空格：

```md
00:00好今天我们来讲和生成式软件工程关系特别大的一个话题
00:06也就是你和 AI 沟通的那个渠道
00:08就是自然语言的提示词
00:10这基本上就是大家和机器沟通的唯一接口
00:15也就是从 ChatGPT 的时代开始
```

解析规则：

- 一条字幕从本行时间开始，到下一行时间结束；
- 最后一条字幕持续到视频结束；
- 没有行首时间戳的行会被忽略；
- 时间戳必须按非递减顺序排列；
- 字幕时间不能超过视频总时长；
- 超过一小时可以写成 `01:02:03`，也可以使用分钟累计格式，例如 `62:03`。

## 四、选择模型

分别配置工作模型和最终模型：

```bash
export CHALKSYNC_WORKER_MODEL="你的工作模型"
export CHALKSYNC_FINAL_MODEL="你的最终模型"
```

两个阶段的职责不同：

- 工作模型：识别视频布局、读取板书/PPT、按时间段输出结构化 JSON；
- 最终模型：同时读取结构化结果和原始时间戳字幕，纠错并生成最终笔记。

也可以不设置以上变量，在每条命令中使用 `--model`、`--worker-model` 或 `--final-model` 明确指定。

## 五、同步执行

### 1. 准备课程

```bash
./scripts/chalksync prepare courses/prompt-engineering
```

该步骤会：

- 检查 `inbox/` 中是否恰好有一份视频和一份 Markdown 字幕；
- 读取视频尺寸和时长；
- 解析字幕时间轴；
- 在 `media/source.*` 创建指向原视频的符号链接；
- 抽取用于识别版面的整帧画面。

### 2. 执行工作阶段

```bash
./scripts/chalksync worker courses/prompt-engineering --mode sync
```

该步骤会先识别板书区、PPT/投影区，再按区域抽帧并分析每个时间段。

每个分段独立写入 `worker/chunk-*.json`。命令中断后可以直接重新执行，已经完成的分段不会重复调用模型。

### 3. 生成最终笔记

```bash
./scripts/chalksync finalize courses/prompt-engineering \
  --web-video-url "https://www.bilibili.com/video/BV1CQt365EzW/"
```

最终笔记位于：

```text
courses/prompt-engineering/notes/course.md
```

笔记中的时间戳会被确定性地转换为网页链接：

```md
[00:12:34](https://www.bilibili.com/video/BV1CQt365EzW/?t=754)
```

点击后直接打开 B 站视频的对应时间。网页地址也会保存到课程的 `course.json`，以后重新生成笔记时无需重复输入。

### 4. 生成并打开播放器

```bash
./scripts/chalksync viewer courses/prompt-engineering
./scripts/chalksync serve courses/prompt-engineering --open
```

默认地址：

```text
http://127.0.0.1:8765/viewer/index.html
```

播放器左侧显示视频，右侧显示笔记。点击笔记中的 `[HH:MM:SS]` 即可跳转到对应视频位置。

### 一条命令完成同步流程

```bash
./scripts/chalksync run courses/prompt-engineering \
  --worker-model "你的工作模型" \
  --final-model "你的最终模型" \
  --web-video-url "视频网页地址"
```

该命令依次执行 `prepare`、同步 `worker`、`finalize` 和 `viewer`。

## 六、使用 Batch 处理工作阶段

当工作模型支持 Batch，并且不要求立即拿到结果时，可以把所有分段作为一个离线任务提交。

### 1. 准备并提交

```bash
./scripts/chalksync prepare courses/prompt-engineering

./scripts/chalksync worker courses/prompt-engineering \
  --model "你的工作模型" \
  --mode batch-submit
```

任务信息保存在：

```text
courses/prompt-engineering/state/worker_batch.json
```

为避免产生重复任务，仓库检测到已有 Batch 记录时会拒绝再次提交。只有明确使用 `--force` 才会创建替代任务。

### 2. 查询并收集结果

```bash
./scripts/chalksync collect courses/prompt-engineering
```

Batch 尚未完成时，该命令只更新状态，不会破坏已有内容。完成后再次执行也是幂等的。

### 3. 生成最终笔记

```bash
./scripts/chalksync finalize courses/prompt-engineering \
  --model "你的最终模型" \
  --web-video-url "视频网页地址"
```

## 七、完成后清理视频和中间文件

先预览将被删除的内容：

```bash
./scripts/chalksync cleanup courses/prompt-engineering \
  --delete-video \
  --dry-run
```

确认无误后执行：

```bash
./scripts/chalksync cleanup courses/prompt-engineering --delete-video
```

该命令会永久删除：

- `inbox/` 中的视频；
- 整帧和板书/PPT 裁剪帧；
- 解析后的字幕副本；
- 分块输入、worker 输出和 Batch 状态；
- 本地播放器和分段草稿。

默认保留：

- `inbox/` 中原始的 `subtitles.md`；
- `course.json`；
- `notes/course.md`；
- `notes/manifest.json`。

删除视频前，脚本会强制检查最终笔记是否存在，并确认其中至少有一个有效的网页时间戳链接。验证不通过时会拒绝删除。该删除不可恢复。

如果希望最终只保留笔记和配置，可以额外使用 `--delete-subtitle`。

同步流程可以在成功生成笔记后自动清理：

```bash
./scripts/chalksync run courses/prompt-engineering \
  --worker-model "你的工作模型" \
  --final-model "你的最终模型" \
  --web-video-url "视频网页地址" \
  --cleanup \
  --delete-video
```

## 八、板书与 PPT 识别机制

视觉处理分为三步：

1. 从整个视频均匀抽取代表性整帧；
2. 工作模型返回板书、PPT、投影或屏幕区域的归一化坐标；
3. `ffmpeg` 按这些坐标重新裁切原视频，分别检测板书变化和投影变化。

布局结果位于：

```text
courses/prompt-engineering/visual/layout.json
```

示例：

```json
{
  "regions": [
    {
      "id": "board",
      "kind": "board",
      "label": "左侧黑板",
      "x": 0.0,
      "y": 0.0,
      "width": 0.58,
      "height": 1.0,
      "confidence": 0.95
    },
    {
      "id": "slides",
      "kind": "slides",
      "label": "右侧投影",
      "x": 0.58,
      "y": 0.0,
      "width": 0.42,
      "height": 1.0,
      "confidence": 0.94
    }
  ]
}
```

坐标范围都是 `0` 到 `1`。如果自动识别不准确，可以手工修改该文件，然后重新运行：

```bash
./scripts/chalksync worker courses/prompt-engineering --mode sync --force
```

工作结果会分别保存：

- `board_content`：板书中可辨认的文字及解释；
- `slide_content`：PPT/投影/屏幕中可辨认的文字及解释；
- `visual_evidence`：其他与课程讲解相关的视觉证据。

无法辨认的文字应标为 `[illegible]`，不会根据字幕擅自补写。

## 九、参数调整

课程参数位于 `course.json`。

常用配置：

```json
{
  "chunk_seconds": 300,
  "visual": {
    "layout_sample_frames": 6,
    "full_frame_interval_seconds": 60,
    "region_interval_seconds": 30,
    "full_scene_threshold": 0.18,
    "board_scene_threshold": 0.025,
    "slides_scene_threshold": 0.08,
    "max_scene_frames": 240,
    "max_frames_per_chunk": 6,
    "image_detail": "high"
  }
}
```

含义：

- `chunk_seconds`：工作模型每次处理的字幕时间长度；
- `layout_sample_frames`：识别画面布局时使用的代表帧数量；
- `region_interval_seconds`：每个板书/PPT 区域的保底抽帧间隔；
- `board_scene_threshold`：板书变化灵敏度，越小越容易抽取细微变化；
- `slides_scene_threshold`：PPT/投影变化灵敏度；
- `max_scene_frames`：单一区域最多保留的变化帧数量；
- `max_frames_per_chunk`：每个字幕分段最多发送的画面数量；
- `image_detail`：画面输入清晰度。板书识别默认使用 `high`。

## 十、输出目录

```text
courses/prompt-engineering/
├── inbox/                         用户上传的视频和字幕
├── course.json                    课程与视觉参数
├── media/
│   ├── source.mp4                 指向 inbox 视频的符号链接
│   └── frames/
│       ├── full/                  用于识别布局的整帧
│       └── regions/               分开的板书/PPT 区域帧
├── transcript/segments.jsonl      解析后的时间戳字幕
├── visual/
│   ├── layout.json                板书/PPT 区域坐标
│   └── frames.json                区域帧时间轴
├── chunks/chunks.json             字幕和画面的分段输入
├── worker/chunk-*.json            工作模型结果
├── state/
│   ├── usage.jsonl                各阶段 token 用量
│   └── worker_batch.json          Batch 状态
├── notes/course.md                最终课程笔记
└── viewer/index.html              本地视频笔记播放器
```

各课程的 `course.json`、课程清单和空 `inbox/` 会保留在仓库中；`inbox/` 内实际上传的视频/字幕，以及抽帧、分析和笔记等生成目录会被 `.gitignore` 排除。

## 十一、常见问题

### 找不到 ffmpeg

错误示例：

```text
Required program 'ffmpeg' was not found on PATH
```

安装 `ffmpeg` 后重新运行 `doctor`。

### inbox 文件数量不正确

`inbox/` 必须恰好包含一份支持格式的视频和一份 `.md` 文件。临时副本、第二个 Markdown 文件也会触发拒绝。

### 字幕没有被解析

确认每条字幕都在独立行，且行首直接使用 `00:00`、`12:34` 或 `01:02:03` 格式。

### 布局识别错误

编辑 `visual/layout.json` 中的坐标，然后用 `worker --force` 重新抽取区域帧。

### 工作模型没有返回合法 JSON

原始响应会保存在：

```text
worker/chunk-XXXX.raw.txt
```

可以检查模型输出能力或调整模型配置，然后重新执行工作阶段。

### 中断后如何继续

不加 `--force` 重新执行原命令即可。已经存在的 `worker/chunk-*.json` 会被跳过。

## 十二、数据边界

- 完整视频始终保留在本地；
- 发送到模型端点的是当前时间段字幕，以及有限数量的板书/PPT 裁剪帧；
- 视频、字幕和生成结果默认位于被 Git 忽略的 `courses/`；
- API key 不会写入仓库；
- 视频和字幕内容只作为学习资料，不会被当作系统指令执行；
- 对敏感课程资料，应使用本地模型或经过批准的 API 地址。
