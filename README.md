# ChalkSync

[中文使用说明](README.zh-CN.md)

ChalkSync is a local-first pipeline that turns a lecture video and timestamp-prefixed Markdown subtitles into evidence-linked course notes. A Worker Stage extracts structured transcript and board/slide evidence; a Final Stage verifies it and writes the study guide.

## Why ChalkSync

ChalkSync began as a Vibe Learning workflow for studying Professor Yanyan Jiang's *Generative Software Engineering* course at Nanjing University. The lectures combine spoken explanations, evolving blackboard work, and projected slides, so a transcript-only summary loses important context. That origin explains the preconfigured 2026 course catalog in `courses/`, as well as the emphasis on board/slide recognition and timestamped evidence links.

The pipeline itself is course-agnostic: any lecture with a local video and timestamp-prefixed Markdown subtitles can use the same workflow.

## Setup

Requirements: Python 3.10+, `ffmpeg`, and `ffprobe`.

```bash
brew install ffmpeg
./scripts/setup-profiles
```

Fill only `API_KEY` in the two ignored local files:

```text
.local-secrets/profiles/
├── ds.env     deepseek-flash, max reasoning
└── gpt.env    gpt-5.6-sol, xhigh reasoning
```

The directory and files must remain mode `700` and `600`. ChalkSync parses a strict `KEY=VALUE` format and never executes profile files as shell code. Real keys must not be placed in tracked examples, course configuration, documentation, or commands.

Verify local configuration without making network requests:

```bash
./scripts/chalksync-profile doctor
```

After filling the keys, explicitly test authentication against each `/models` endpoint:

```bash
./scripts/chalksync-profile doctor --probe
```

## Profiles

Either profile can be assigned to either stage. The default is DeepSeek worker plus GPT final.

| Profile | Endpoint | Model | Effort | Wire API | Batch |
| --- | --- | --- | --- | --- | --- |
| `ds` | `https://api.deepseek.com` | `deepseek-flash` | `max` | Responses | no |
| `gpt` | `https://xcode.best/v1` | `gpt-5.6-sol` | `xhigh` | Responses | configured yes |

Both use native Responses APIs. Structured layout/worker requests use JSON mode. DeepSeek-bound images are normalized locally to the configured 640,000-pixel and 1 MiB policy without lowering reusable source-frame quality.

`xcode.best` is a third-party endpoint. Course evidence sent there is governed by that service's privacy and retention behavior; `store: false` is not a substitute for the operator's policy.

## Run

Create a course, then place exactly one supported video and one timestamped Markdown subtitle in its `inbox/`:

```bash
./scripts/chalksync-profile init courses/prompt-engineering --title "Prompt Engineering"
```

The two input filenames are arbitrary and do not need matching stems or any relationship to the course title. For example, `lecture.mp4` and `subtitles.md` are valid together. The course title is set only by `--title`. The video extension must be `.mp4`, `.mkv`, `.webm`, `.mov`, or `.m4v`; the subtitle extension must be `.md`.

Run the default `ds/gpt` pipeline:

```bash
./scripts/chalksync-profile run courses/prompt-engineering \
  --web-video-url "https://www.bilibili.com/video/BV1CQt365EzW/"
```

The command prints each stage, chunk progress, and model-call duration immediately, including status before long frame scans or API waits:

```text
[prepare] 解析字幕完成：2218 条
[frames] 正在分析 PPT 区域：右侧投影...
[worker] 2/20 完成，用时 48.0 秒
[worker] 3/20 等待 DeepSeek (deepseek-flash) 响应...
[final] 等待 GPT (gpt-5.6-sol) 生成最终笔记...
```

On resumed runs, provenance-validated Worker chunks and Final section drafts are reported as reused. If a long Final request returns HTTP 524, rerun with the same arguments to continue, or reduce each request with `--max-source-characters 60000`; do not add `--force` when the goal is to resume.

Choose either profile per stage:

```bash
./scripts/chalksync-profile run courses/prompt-engineering \
  --worker gpt \
  --final ds
```

Run stages independently when needed:

```bash
./scripts/chalksync-profile prepare courses/prompt-engineering
./scripts/chalksync-profile worker courses/prompt-engineering --profile ds --mode sync
./scripts/chalksync-profile finalize courses/prompt-engineering --profile gpt
./scripts/chalksync-profile serve courses/prompt-engineering --open
```

The pipeline never silently switches providers, retries incomplete reasoning responses, or weakens JSON mode. Generated artifacts record non-secret profile provenance. A changed endpoint, model, effort, token limit, prompt revision, or input stops before an API call and requires an explicit `--force` regeneration.

Changing only the Final Profile preserves worker artifacts. Changing the Worker Profile preserves prepared input but requires worker and final regeneration.

## Batch

The DeepSeek profile is synchronous only. A GPT endpoint that implements OpenAI Files, Batch, and Responses may use:

```bash
./scripts/chalksync-profile worker courses/prompt-engineering \
  --profile gpt \
  --mode batch-submit
./scripts/chalksync-profile collect courses/prompt-engineering
```

The default synchronous `run` command does not depend on Batch support. Confirm that a third-party GPT endpoint implements the complete Batch surface before using it.

## Data boundary

- The full video remains local.
- The Worker Stage sends bounded subtitle chunks and selected board/slide frames.
- The Final Stage sends worker evidence and the timestamped transcript.
- API keys are never included in provenance, course configuration, logs, or requests bodies.
- `.local-secrets/` is ignored by Git, but a key that was ever committed must still be revoked.

See [README.zh-CN.md](README.zh-CN.md) for the full workflow, cleanup behavior, cache rules, and troubleshooting.
