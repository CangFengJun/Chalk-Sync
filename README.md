# ChalkSync

[中文使用说明](README.zh-CN.md)

ChalkSync is a local-first pipeline that turns a lecture video and timestamp-prefixed Markdown subtitles into evidence-linked course notes. A Worker Stage extracts structured transcript and board/slide evidence; a Final Stage verifies it and writes the study guide.

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

Run the default `ds/gpt` pipeline:

```bash
./scripts/chalksync-profile run courses/prompt-engineering \
  --web-video-url "https://www.bilibili.com/video/BV1CQt365EzW/"
```

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
