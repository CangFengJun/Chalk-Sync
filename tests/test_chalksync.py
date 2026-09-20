from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from chalksync.cleanup import cleanup_course
from chalksync.openai_api import response_text
from chalksync.pipeline import (
    _select_chunk_frames,
    _validate_layout,
    finalize_course,
    init_course,
    prepare_course,
)
from chalksync.transcript import chunk_segments, parse_markdown_subtitle
from chalksync.utils import (
    extract_json_object,
    format_timestamp,
    link_markdown_timestamps,
    timestamp_url,
)
from chalksync.viewer import build_viewer


SAMPLE_SUBTITLES = """00:00好今天我们来讲和深圳市软件工程里面非常关系特别大的一个
00:06也就是你和AI沟通的那个渠道
00:08就是自然语言的提示词
00:10这基本上就是呃大家和机器沟通的唯一的接口
00:15也就是从ChatGPT的时代
"""


class SubtitleTests(unittest.TestCase):
    def test_compact_markdown_subtitles_infer_end_times(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "subtitles.md"
            path.write_text(SAMPLE_SUBTITLES, encoding="utf-8")
            segments = parse_markdown_subtitle(path, video_duration=20)
        self.assertEqual([item["start"] for item in segments], [0, 6, 8, 10, 15])
        self.assertEqual([item["end"] for item in segments], [6, 8, 10, 15, 20])
        self.assertEqual(segments[1]["text"], "也就是你和AI沟通的那个渠道")

    def test_hour_timestamp_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "subtitles.md"
            path.write_text("01:02:03一小时后的内容\n01:02:10下一句\n", encoding="utf-8")
            segments = parse_markdown_subtitle(path, video_duration=4000)
        self.assertEqual(segments[0]["start"], 3723)
        self.assertEqual(segments[0]["end"], 3730)

    def test_decreasing_timestamps_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "subtitles.md"
            path.write_text("00:10后一句\n00:05前一句\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "non-decreasing"):
                parse_markdown_subtitle(path)

    def test_timestamp_after_video_end_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "subtitles.md"
            path.write_text("00:30超出视频\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "after video duration"):
                parse_markdown_subtitle(path, video_duration=10)


class PipelineTests(unittest.TestCase):
    def test_existing_notes_can_be_linked_without_model_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            course = Path(temporary) / "course"
            init_course(course, title="Test Course")
            notes = course / "notes" / "course.md"
            notes.parent.mkdir(parents=True)
            notes.write_text("在 [00:12:34] 讨论关键概念。\n", encoding="utf-8")
            result = finalize_course(
                course,
                client=object(),  # Existing notes make the API client unnecessary.
                model="user-selected-model",
                web_video_url="https://www.bilibili.com/video/BVTEST/",
            )
            content = result.read_text(encoding="utf-8")
            manifest = json.loads((course / "notes" / "manifest.json").read_text())
        self.assertIn("BVTEST/?t=754", content)
        self.assertEqual(manifest["timestamp_links"], 1)

    def test_prepare_uses_video_and_markdown_from_inbox(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            course = Path(temporary) / "course"
            init_course(course, title="Test Course")
            (course / "inbox" / "lecture.mp4").write_bytes(b"not-a-real-video")
            (course / "inbox" / "subtitles.md").write_text(
                SAMPLE_SUBTITLES, encoding="utf-8"
            )
            fake_media = {
                "path": str(course / "media" / "source.mp4"),
                "duration": 20.0,
                "size": 16,
                "format_name": "mp4",
                "width": 1920,
                "height": 1080,
                "codec": "h264",
            }
            fake_frames = [
                {
                    "time": 0.0,
                    "path": "media/frames/full/periodic_000001.jpg",
                    "kind": "periodic",
                }
            ]
            with patch("chalksync.pipeline.probe_media", return_value=fake_media), patch(
                "chalksync.pipeline.extract_frames", return_value=fake_frames
            ):
                config = prepare_course(course)
            transcript = (course / "transcript" / "segments.jsonl").read_text(encoding="utf-8")
        self.assertEqual(config["transcript"]["segments"], 5)
        self.assertIn("ChatGPT", transcript)

    def test_layout_is_clamped_and_kinds_are_normalized(self) -> None:
        result = _validate_layout(
            {
                "regions": [
                    {
                        "id": "Black Board",
                        "kind": "board",
                        "x": 0.98,
                        "y": -1,
                        "width": 0.5,
                        "height": 2,
                        "confidence": 1.4,
                    }
                ]
            }
        )
        region = result["regions"][0]
        self.assertEqual(region["id"], "black-board")
        self.assertLessEqual(region["x"] + region["width"], 1)
        self.assertLessEqual(region["y"] + region["height"], 1)
        self.assertEqual(region["confidence"], 1)

    def test_frame_selection_balances_board_and_slides(self) -> None:
        frames = [
            {"time": value, "path": f"b{value}", "region_kind": "board"}
            for value in range(0, 100, 10)
        ] + [
            {"time": value, "path": f"s{value}", "region_kind": "slides"}
            for value in range(0, 100, 10)
        ]
        selected = _select_chunk_frames(frames, 0, 100, 6)
        self.assertEqual(len(selected), 6)
        self.assertEqual({item["region_kind"] for item in selected}, {"board", "slides"})

    def test_chunks_keep_timestamped_transcript(self) -> None:
        segments = [
            {"start": 0, "end": 6, "text": "first"},
            {"start": 61, "end": 64, "text": "second"},
        ]
        chunks = chunk_segments(segments, duration=120, chunk_seconds=60)
        self.assertEqual(len(chunks), 2)
        self.assertIn("[00:00:00] first", chunks[0]["transcript"])
        self.assertIn("[00:01:01] second", chunks[1]["transcript"])

    def test_viewer_contains_video_and_timestamp_controls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            course = Path(temporary) / "course"
            config = init_course(course, title="Test Course")
            config["media"] = {"course_video": "media/source.mp4"}
            (course / "course.json").write_text(
                json.dumps(config, ensure_ascii=False), encoding="utf-8"
            )
            output = build_viewer(course)
            html = output.read_text(encoding="utf-8")
        self.assertIn("../media/source.mp4", html)
        self.assertIn("currentTime", html)
        self.assertIn("Test Course", html)


class UtilityTests(unittest.TestCase):
    def test_json_extraction_and_response_text(self) -> None:
        parsed = extract_json_object("```json\n{\"ok\": true}\n```")
        self.assertTrue(parsed["ok"])
        response = {
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": "done"}]}
            ]
        }
        self.assertEqual(response_text(response), "done")
        self.assertEqual(format_timestamp(3723), "01:02:03")

    def test_timestamp_links_preserve_query_and_replace_existing_links(self) -> None:
        base = "https://www.bilibili.com/video/BVTEST/?p=2&t=1"
        self.assertEqual(
            timestamp_url(base, 754),
            "https://www.bilibili.com/video/BVTEST/?p=2&t=754",
        )
        markdown, count = link_markdown_timestamps(
            "A [00:00:06] B [00:12:34](https://old.example/?t=1)", base
        )
        self.assertEqual(count, 2)
        self.assertIn("p=2&t=6", markdown)
        self.assertIn("p=2&t=754", markdown)


class CleanupTests(unittest.TestCase):
    def test_cleanup_requires_links_before_deleting_video(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            course = Path(temporary) / "course"
            init_course(
                course,
                title="Test Course",
                web_video_url="https://www.bilibili.com/video/BVTEST/",
            )
            notes = course / "notes" / "course.md"
            notes.parent.mkdir(parents=True)
            notes.write_text("只有原始时间 [00:00:06]\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "verified web timestamp links"):
                cleanup_course(course, delete_video=True)

    def test_cleanup_deletes_video_and_intermediates_but_keeps_notes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            course = Path(temporary) / "course"
            init_course(
                course,
                title="Test Course",
                web_video_url="https://www.bilibili.com/video/BVTEST/",
            )
            video = course / "inbox" / "lecture.mp4"
            subtitle = course / "inbox" / "subtitles.md"
            video.write_bytes(b"video-bytes")
            subtitle.write_text("00:00字幕\n", encoding="utf-8")
            for name in ("media", "transcript", "visual", "chunks", "worker", "state", "viewer"):
                directory = course / name
                directory.mkdir()
                (directory / "artifact.bin").write_bytes(b"x" * 10)
            notes = course / "notes" / "course.md"
            notes.parent.mkdir(parents=True)
            notes.write_text(
                "[00:00:06](https://www.bilibili.com/video/BVTEST/?t=6) 内容\n",
                encoding="utf-8",
            )
            (course / "notes" / "manifest.json").write_text("{}", encoding="utf-8")

            preview = cleanup_course(course, delete_video=True, dry_run=True)
            self.assertTrue(video.exists())
            self.assertGreater(preview["bytes_reclaimed"], 0)

            report = cleanup_course(course, delete_video=True)
            self.assertFalse(video.exists())
            self.assertFalse((course / "media").exists())
            self.assertTrue(subtitle.exists())
            self.assertTrue(notes.exists())
            self.assertEqual(report["timestamp_links"], 1)


if __name__ == "__main__":
    unittest.main()
