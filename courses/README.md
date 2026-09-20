# 2026 南京大学《生成式软件工程》课程目录

以下正式课程已经预配置。每个 `inbox/` 需要用户放入一份视频和一份带时间戳的 Markdown 字幕。

| 课次 | 发布日期 | 课程 | 上传目录 |
| --- | --- | --- | --- |
| 01 | 2026-08-27 | 欢迎来到未来 | `welcome-to-the-future/inbox/` |
| 02 | 2026-09-01 | 提示词工程 | `prompt-engineering/inbox/` |
| 03 | 2026-09-08 | 软件仓库管理 | `repository-management/inbox/` |
| 04 | 2026-09-15 | 软件仓库管理（2） | `repository-management-2/inbox/` |
| 05 | 2026-09-20 | 软件工程的来龙去脉 | `software-engineering-origins/inbox/` |

未配置 2026-08-28 发布的短片《失败的 AI Slop 数字永生尝试》。

字幕格式示例：

```md
00:00课程开场
00:06第一个知识点
00:15进一步解释
```

处理单门课程：

```bash
./scripts/chalksync prepare courses/<课程目录>
./scripts/chalksync worker courses/<课程目录> --mode sync
./scripts/chalksync finalize courses/<课程目录>
```
