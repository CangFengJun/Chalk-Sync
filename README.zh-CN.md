# ChalkSync 中文使用说明

ChalkSync 把课程视频和带时间戳的 Markdown 字幕加工成可追溯的课程笔记。它会识别板书/PPT 区域、按时间段整理字幕与画面证据，再用最终模型核验并生成带时间戳的完整笔记。

## 项目缘起

ChalkSync 最初是为了用 Vibe Learning 的方式学习蒋炎岩老师讲授的南京大学《生成式软件工程》课程而开发的。这门课的知识不仅来自讲述，也分布在持续变化的板书和投影/PPT 中，单纯总结字幕会丢失重要上下文。因此，仓库预先配置了 2026 年课程目录，并把板书/PPT 区域识别、视觉证据提取和可跳转的时间戳链接作为核心能力。

处理流程本身并不局限于这门课：只要提供本地视频和带时间戳的 Markdown 字幕，也可以用于其他课程。

## 处理流程

```text
视频 + 字幕
    |
    v
prepare: 校验媒体、解析字幕、抽取整帧
    |
    v
Worker Stage: 识别布局并生成分段证据 JSON
    |
    v
Final Stage: 对照字幕核验并生成 Markdown 笔记
    |
    v
course.md + 本地视频/笔记播放器
```

完整视频始终留在本地。Worker Stage 会把当前字幕分段和有限数量的板书/PPT 图片发送给所选 endpoint；Final Stage 会发送完整字幕证据与 worker 结果。

## 一、准备环境

需要：

- Python 3.10 或更高版本；
- `ffmpeg` 和 `ffprobe`；
- DeepSeek 与 GPT endpoint 各自可用的 API key。

macOS 安装媒体工具：

```bash
brew install ffmpeg
```

初始化本地 profile：

```bash
./scripts/setup-profiles
```

该命令创建两个不会被 Git 跟踪的文件：

```text
.local-secrets/profiles/
├── ds.env
└── gpt.env
```

打开文件并只填写各自的 `API_KEY`：

```text
API_KEY=''
```

不要把真实 key 写入 `config/profiles/*.example`、`course.json`、README 或命令行。实际目录权限为 `700`，profile 文件权限为 `600`；解析器只读取白名单内的 `KEY=VALUE` 字段，不会把文件当 shell 脚本执行。

检查本地环境：

```bash
./scripts/chalksync-profile doctor
```

填写 key 后，可以显式检查两个 endpoint 的认证。该检查只读取 `/models`，不生成课程内容：

```bash
./scripts/chalksync-profile doctor --probe
```

## 二、模型 Profile

两个 profile 都能分配给 Worker Stage 或 Final Stage，默认组合是 `ds/gpt`：

| Profile | 默认 endpoint | 模型 | 推理强度 | 协议 | Batch |
| --- | --- | --- | --- | --- | --- |
| `ds` | `https://api.deepseek.com` | `deepseek-flash` | `max` | Responses | 禁用 |
| `gpt` | `https://xcode.best/v1` | `gpt-5.6-sol` | `xhigh` | Responses | 配置为启用 |

可在本地 profile 中调整模型、endpoint、推理强度和 token 上限。字段改变后，已有生成物的 provenance 不再匹配，ChalkSync 会在调用 API 前停止，并要求明确使用 `--force`。

DeepSeek 图片会在请求构造阶段压缩到本地策略规定的 640,000 像素和 1 MiB 以内；原始抽帧不会被降质。布局和 worker 请求启用服务端 JSON 模式，最终笔记保持 Markdown 文本输出。

两个 provider 都使用原生 Responses API：

- DeepSeek：[Responses API 指南](https://api-docs.deepseek.com/guides/responses_api)
- OpenAI：[Responses API](https://platform.openai.com/docs/api-reference/responses)

`xcode.best` 是第三方 endpoint。课程内容会发送给该服务，其存储与隐私行为取决于服务方；请求中的 `store: false` 不能替代服务方承诺。

## 三、准备课程

每门课程使用独立目录：

```bash
./scripts/chalksync-profile init courses/prompt-engineering \
  --title "提示词工程 [02-Raw/26生成式软件工程/NJU]"
```

在课程的 `inbox/` 中放入且只放入一份视频和一份 `.md` 字幕：

```text
courses/prompt-engineering/inbox/
├── lecture.mp4
└── subtitles.md
```

视频和字幕的文件名可以自行决定，不要求两者同名，也不要求与课程标题一致。例如，`第一讲.mp4` 搭配 `课堂字幕.md` 可以正常使用。课程标题只由初始化时的 `--title` 决定。

支持 `.mp4`、`.mkv`、`.webm`、`.mov`、`.m4v`。

每行字幕以 `MM:SS` 或 `HH:MM:SS` 开头：

```md
00:00好今天我们来讲生成式软件工程
00:06也就是你和 AI 沟通的渠道
00:10这基本上是大家和机器沟通的接口
```

一条字幕从本行时间持续到下一行时间；最后一条持续到视频结束。时间戳必须按非递减顺序排列，不能超过视频总时长。

## 四、一条命令运行

默认使用 DeepSeek worker 和 GPT final：

```bash
./scripts/chalksync-profile run courses/prompt-engineering \
  --web-video-url "https://www.bilibili.com/video/BV1CQt365EzW/"
```

命令会即时显示当前阶段、分块进度和单次模型调用耗时；长时间抽帧或等待 API 时也会先打印状态：

```text
[prepare] 解析字幕完成：2218 条
[frames] 正在分析 PPT 区域：右侧投影...
[worker] 2/20 完成，用时 48.0 秒
[worker] 3/20 等待 DeepSeek (deepseek-flash) 响应...
[final] 等待 GPT (gpt-5.6-sol) 生成最终笔记...
```

重新执行命令时，已通过 provenance 校验的缓存也会逐项显示为“使用已有结果”。

也可以自由组合：

```bash
./scripts/chalksync-profile run courses/prompt-engineering \
  --worker gpt \
  --final ds
```

ChalkSync 不会在 provider 失败时自动切换到另一个 provider，也不会自动重试不完整的推理响应。这避免改变费用、数据接收方或结果来源。

## 五、分阶段运行

准备媒体与字幕：

```bash
./scripts/chalksync-profile prepare courses/prompt-engineering
```

执行 Worker Stage：

```bash
./scripts/chalksync-profile worker courses/prompt-engineering \
  --profile ds \
  --mode sync
```

执行 Final Stage：

```bash
./scripts/chalksync-profile finalize courses/prompt-engineering \
  --profile gpt \
  --web-video-url "https://www.bilibili.com/video/BV1CQt365EzW/"
```

生成并打开播放器：

```bash
./scripts/chalksync-profile viewer courses/prompt-engineering
./scripts/chalksync-profile serve courses/prompt-engineering --open
```

默认地址为 `http://127.0.0.1:8765/viewer/index.html`。

## 六、续跑、切换与 `--force`

每个 worker 分段和 Final 分段草稿都会独立保存；中断后用相同命令及相同参数重跑，匹配的结果会被复用。Final 分段保存在 `notes/sections/section-*.md`，对应的 `.manifest.json` 用于校验模型、输入和 prompt provenance。若最终合并失败，重跑会复用全部已完成分段，只重试合并。

- 只切换 Final Profile：保留媒体、布局、分段和 worker 结果；给 `finalize` 使用 `--force`。
- 切换 Worker Profile：保留输入视频、字幕和准备阶段整帧；给 `worker` 使用 `--force`，随后给 `finalize` 使用 `--force`。
- profile、endpoint、模型、推理强度、token 上限、提示版本或输入发生变化时，不带 `--force` 会在网络请求前停止。
- `--force` 可能产生新的 API 费用，因此不会被自动添加。

失败信息写入 `state/last_error.json`，其中不包含 key、请求正文或课程内容。若响应因 `max_output_tokens` 不完整，请调整对应 profile 的 token 上限后明确重跑。

## 七、Batch

DeepSeek 官方 profile 首版只支持同步 Worker Stage。对声明支持 OpenAI Files/Batch/Responses 的 GPT endpoint，可以执行：

```bash
./scripts/chalksync-profile worker courses/prompt-engineering \
  --profile gpt \
  --mode batch-submit

./scripts/chalksync-profile collect courses/prompt-engineering
```

`collect` 会从 Batch 状态读取提交时的 profile，并验证 endpoint provenance。`xcode.best` 是否完整实现 Files 和 Batch 需要由该服务确认；默认同步 `run` 不依赖 Batch。

## 八、输出

```text
courses/prompt-engineering/
├── inbox/                         原始视频和字幕
├── course.json                    课程与视觉参数，不含 provider/key
├── media/                         本地视频链接和抽帧
├── transcript/segments.jsonl      时间戳字幕
├── visual/                        布局、区域帧和 provenance
├── chunks/chunks.json             worker 输入分段
├── worker/chunk-*.json            worker 结构化结果
├── state/                         用量、Batch 状态和非敏感错误信息
├── notes/sections/                可断点续跑的 Final 分段与 manifest
├── notes/course.md                最终课程笔记
├── notes/manifest.json            最终 provenance
└── viewer/index.html              本地播放器
```

笔记中的 `[HH:MM:SS]` 会确定性地转换为视频网页链接。profile 名称与非敏感 endpoint/model 设置会记录在 provenance 中，API key 不会写入任何生成物。

## 九、清理

先预览：

```bash
./scripts/chalksync-profile cleanup courses/prompt-engineering \
  --delete-video \
  --dry-run
```

确认后执行：

```bash
./scripts/chalksync-profile cleanup courses/prompt-engineering --delete-video
```

删除视频前会检查最终笔记中是否存在有效网页时间戳。笔记、manifest、课程配置和原始字幕默认保留。

## 十、数据与安全边界

- `.local-secrets/` 整个目录被 `.gitignore` 排除；`git add .` 不会加入两个 profile。
- Git 忽略只保护从未被跟踪的文件；若 key 曾经提交，必须立即撤销并更换 key。
- DeepSeek Worker Stage 会收到字幕分段与有限图片。
- GPT Final Stage 会收到 worker 结果与完整时间戳字幕。
- 完整视频不上传。
- 不会自动回退 provider，也不会自动降低 JSON 输出保证。
- 敏感课程只应发送到你信任并获准使用的 endpoint。

## 十一、常见问题

### `doctor` 显示 `API_KEY is empty`

分别打开 `.local-secrets/profiles/ds.env` 和 `gpt.env`，填写对应 key，不要修改示例模板。

### 缓存 provenance 不匹配

确认确实希望按当前 profile 重新付费生成，然后对相应阶段添加 `--force`。不要删除 provenance 字段绕过检查。

### Final 返回 HTTP 524

这通常表示第三方网关等待模型响应超时。不要添加 `--force`；直接重跑会复用全部 Worker 结果和已经完成的 Final 分段。若同一分段反复超时，可减小单次 Final 输入，例如：

```bash
./scripts/chalksync-profile run courses/prompt-engineering \
  --max-source-characters 60000 \
  --web-video-url "https://www.bilibili.com/video/BV1CQt365EzW/"
```

更小的数值会产生更多、但更短的 Final 请求。续跑时必须继续使用相同数值，否则分段方式和 provenance 会改变。

### DeepSeek Batch 被拒绝

这是预期行为。使用 `--mode sync`，或明确选择一个实现 Files/Batch/Responses 的 GPT endpoint。

### 工作模型没有返回合法 JSON

结构化输出仍会经过本地 JSON 校验。无法解析的原始文本保存在 `worker/chunk-XXXX.raw.txt`，不会被当作成功结果缓存。
