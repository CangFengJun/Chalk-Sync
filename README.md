# ChalkSync

[中文使用说明](README.zh-CN.md)

Turn lectures into notes that know where they came from.

ChalkSync is a local-first pipeline for turning a lecture video and timestamp-prefixed Markdown subtitles into evidence-linked course notes.

The repository handles frame extraction, board/slide layout detection, region-specific visual analysis, resumable chunk processing, final verification, and a synchronized local viewer. The user chooses all API models.

## Why ChalkSync

ChalkSync began as a Vibe Learning workflow for studying Professor Yanyan Jiang's *Generative Software Engineering* course at Nanjing University. The lectures combine spoken explanations, evolving blackboard work, and projected slides, so a transcript-only summary loses important context. That origin explains the preconfigured 2026 course catalog in `courses/`, as well as the emphasis on board/slide recognition and timestamped evidence links.

The pipeline itself is course-agnostic: any lecture with a local video and timestamp-prefixed Markdown subtitles can use the same workflow.

## Input contract

Initialize one directory per course:

```bash
./scripts/chalksync init courses/prompt-engineering --title "提示词工程"
```

Then place exactly two files in the generated `inbox/`:

```text
courses/prompt-engineering/inbox/
├── lecture.mp4
└── subtitles.md
```

Subtitle lines start with `MM:SS` or `HH:MM:SS`; whitespace after the timestamp is optional:

```md
00:00好今天我们来讲和生成式软件工程关系特别大的一个话题
00:06也就是你和 AI 沟通的那个渠道
00:08就是自然语言的提示词
00:10这基本上就是大家和机器沟通的唯一接口
00:15也就是从 ChatGPT 的时代开始
```

Each line ends when the next timestamp begins. The final line ends at the video duration. Lines without a leading timestamp are ignored.

## Pipeline

```text
inbox video + subtitles.md
        |
        v
prepare: validate, probe, parse timestamps, sample full frames
        |
        v
layout: worker model locates board and projected slide/screen regions
        |
        v
region extraction: ffmpeg samples periodic and visual-change frames per region
        |
        v
worker: subtitle chunks + board frames + slide frames -> structured JSON
        |
        v
final: final model checks worker claims against original timestamped subtitles
        |
        v
course.md + local video/notes viewer
```

The worker stage keeps each chunk in a separate file, so interrupted runs resume without repeating completed work. Course content is always treated as untrusted source material, never as instructions.

The full video never goes to the model endpoint. Worker requests do transmit the relevant subtitle chunk and a bounded set of cropped board/slide frames to the endpoint configured by the user. Use a local or approved endpoint when course material is sensitive.

## Prerequisites

- Python 3.9+
- `ffmpeg` and `ffprobe`
- `OPENAI_API_KEY`, or an API-compatible endpoint configured with `OPENAI_BASE_URL`

Check the machine without changing it:

```bash
./scripts/chalksync doctor
```

This project has no Python package dependencies. Installing it is optional; all examples use the repository wrapper script.

## Run synchronously

You choose the model names:

```bash
export CHALKSYNC_WORKER_MODEL="your-worker-model"
export CHALKSYNC_FINAL_MODEL="your-final-model"

./scripts/chalksync prepare courses/prompt-engineering
./scripts/chalksync worker courses/prompt-engineering --mode sync
./scripts/chalksync finalize courses/prompt-engineering
./scripts/chalksync viewer courses/prompt-engineering
./scripts/chalksync serve courses/prompt-engineering --open
```

Pass `--web-video-url URL` to `finalize` (or `run`) to convert every `[HH:MM:SS]` citation into a clickable `URL?t=<seconds>` link. After verifying the notes, reclaim local storage with:

```bash
./scripts/chalksync cleanup courses/prompt-engineering --delete-video --dry-run
./scripts/chalksync cleanup courses/prompt-engineering --delete-video
```

Cleanup preserves `notes/course.md`, its manifest, course configuration, and the original Markdown subtitle by default.

Or run the synchronous path end to end:

```bash
./scripts/chalksync run courses/prompt-engineering \
  --worker-model "your-worker-model" \
  --final-model "your-final-model"
```

Do not paste an API key into these commands. Set it in the environment or your secret manager.

## Run the worker stage as a Batch job

For offline processing, prepare and submit all chunk requests as one Batch job:

```bash
./scripts/chalksync prepare courses/prompt-engineering
./scripts/chalksync worker courses/prompt-engineering \
  --model "your-worker-model" \
  --mode batch-submit
```

Later, collect the job. Repeating `collect` is safe while it is still running:

```bash
./scripts/chalksync collect courses/prompt-engineering
./scripts/chalksync finalize courses/prompt-engineering \
  --model "your-final-model"
```

The exact model and endpoint must support the requested modality and operation. The pipeline does not silently switch models.

## Artifacts

```text
course/
├── inbox/                         User-owned video and subtitles
├── course.json                    Pipeline configuration
├── media/
│   ├── source.mp4                 Symlink to the inbox video
│   └── frames/
│       ├── full/                  Layout-detection frames
│       └── regions/               Separate board/slide frames
├── transcript/segments.jsonl      Parsed timestamp evidence
├── visual/
│   ├── layout.json                Normalized region coordinates
│   └── frames.json                Region frame timeline
├── chunks/chunks.json             Aligned subtitle and image inputs
├── worker/chunk-*.json            Resumable worker outputs
├── state/                         Batch state and token usage
├── notes/course.md                Final study guide
└── viewer/index.html              Synchronized local viewer
```

Delete or edit `visual/layout.json` only when automatic layout detection is wrong, then rerun the worker command with `--force`. The coordinates use normalized `x`, `y`, `width`, and `height` values from `0` to `1`.
