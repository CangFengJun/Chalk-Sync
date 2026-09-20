from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import threading
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

from chalksync.cleanup import cleanup_course
from chalksync.openai_api import (
    ResponsesAPIError,
    ResponsesClient,
    _image_dimensions,
    response_payload,
    response_text,
)
from chalksync.pipeline import (
    _select_chunk_frames,
    _validate_layout,
    finalize_course,
    init_course,
    prepare_course,
    run_workers_sync,
)
from chalksync.profiles import ModelProfile, ProfileError, load_profile
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


def model_profile(
    name: str = "ds",
    *,
    provider: str = "deepseek",
    api_key: str = "test-key",
    base_url: str = "https://api.deepseek.com",
    model: str = "deepseek-flash",
    reasoning_effort: str = "max",
    batch_supported: bool = False,
    image_policy: bool = True,
) -> ModelProfile:
    return ModelProfile(
        name=name,
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        wire_api="responses",
        model=model,
        reasoning_effort=reasoning_effort,
        batch_supported=batch_supported,
        image_max_pixels=640_000 if image_policy else None,
        image_max_bytes=1_048_576 if image_policy else None,
        layout_max_output_tokens=12_000,
        worker_max_output_tokens=16_000,
        final_max_output_tokens=30_000,
    )


def write_profile(root: Path, name: str, api_key: str = "test-key") -> Path:
    secrets = root / ".local-secrets"
    profiles = secrets / "profiles"
    profiles.mkdir(parents=True)
    os.chmod(secrets, 0o700)
    os.chmod(profiles, 0o700)
    path = profiles / f"{name}.env"
    path.write_text(
        "\n".join(
            [
                "PROVIDER='deepseek'",
                f"API_KEY='{api_key}'",
                "BASE_URL='https://api.deepseek.com'",
                "WIRE_API='responses'",
                "MODEL='deepseek-flash'",
                "REASONING_EFFORT='max'",
                "BATCH_SUPPORTED='false'",
                "IMAGE_MAX_PIXELS='640000'",
                "IMAGE_MAX_BYTES='1048576'",
                "LAYOUT_MAX_OUTPUT_TOKENS='12000'",
                "WORKER_MAX_OUTPUT_TOKENS='16000'",
                "FINAL_MAX_OUTPUT_TOKENS='30000'",
                "",
            ]
        ),
        encoding="utf-8",
    )
    os.chmod(path, 0o600)
    return path


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
    def test_existing_notes_without_provenance_are_not_reused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            course = Path(temporary) / "course"
            init_course(course, title="Test Course")
            notes = course / "notes" / "course.md"
            notes.parent.mkdir(parents=True)
            notes.write_text("在 [00:12:34] 讨论关键概念。\n", encoding="utf-8")
            chunks = course / "chunks" / "chunks.json"
            chunks.parent.mkdir()
            chunks.write_text("[]\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "provenance"):
                finalize_course(
                    course,
                    client=ResponsesClient(model_profile()),
                    web_video_url="https://www.bilibili.com/video/BVTEST/",
                )

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

    def test_two_profiles_run_end_to_end_over_responses_http(self) -> None:
        requests: list[dict] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length))
                requests.append(
                    {
                        "path": self.path,
                        "authorization": self.headers.get("Authorization"),
                        "payload": payload,
                    }
                )
                instructions = str(payload.get("instructions", ""))
                if "identify stable content regions" in instructions:
                    text = json.dumps(
                        {
                            "regions": [
                                {
                                    "id": "screen",
                                    "kind": "screen",
                                    "label": "full frame",
                                    "x": 0,
                                    "y": 0,
                                    "width": 1,
                                    "height": 1,
                                    "confidence": 1,
                                }
                            ],
                            "uncertainties": [],
                        }
                    )
                elif "extract evidence" in instructions:
                    text = json.dumps({"summary": "evidence", "uncertainties": []})
                else:
                    text = "# Test Notes\n\n[00:00:00] Verified."
                body = json.dumps(
                    {
                        "status": "completed",
                        "output": [
                            {
                                "type": "message",
                                "content": [{"type": "output_text", "text": text}],
                            }
                        ],
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            ds = replace(
                model_profile(api_key="ds-secret", image_policy=False),
                base_url=base_url,
            )
            gpt = model_profile(
                name="gpt",
                provider="gpt",
                api_key="gpt-secret",
                base_url=base_url,
                model="gpt-5.6-sol",
                reasoning_effort="xhigh",
                image_policy=False,
            )
            with tempfile.TemporaryDirectory() as temporary:
                course = Path(temporary) / "course"
                config = init_course(course, title="HTTP Test")
                config["visual"].update(
                    {
                        "full_frame_interval_seconds": 1,
                        "region_interval_seconds": 1,
                        "max_scene_frames": 4,
                    }
                )
                (course / "course.json").write_text(
                    json.dumps(config, ensure_ascii=False), encoding="utf-8"
                )
                subprocess.run(
                    [
                        "ffmpeg",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-f",
                        "lavfi",
                        "-i",
                        "color=c=white:s=320x180:d=2",
                        "-pix_fmt",
                        "yuv420p",
                        str(course / "inbox" / "lecture.mp4"),
                    ],
                    check=True,
                )
                (course / "inbox" / "subtitles.md").write_text(
                    "00:00HTTP integration test\n", encoding="utf-8"
                )
                fake_media = {
                    "path": str(course / "media" / "source.mp4"),
                    "duration": 2.0,
                    "size": 1,
                    "format_name": "mp4",
                    "width": 320,
                    "height": 180,
                    "codec": "h264",
                }

                def fake_extract_frames(video, output_dir, **kwargs):
                    output_dir.mkdir(parents=True, exist_ok=True)
                    image = output_dir / "periodic_000001.jpg"
                    image.write_bytes(b"test-image")
                    result = [{
                        "time": 0.0,
                        "path": str(image.relative_to(course.resolve())),
                        "kind": "periodic",
                    }]
                    (output_dir / "index.json").write_text(
                        json.dumps(result), encoding="utf-8"
                    )
                    return result

                with patch("chalksync.pipeline.probe_media", return_value=fake_media), patch(
                    "chalksync.pipeline.extract_frames", side_effect=fake_extract_frames
                ):
                    prepare_course(course)
                    run_workers_sync(course, client=ResponsesClient(ds))
                notes = finalize_course(course, client=ResponsesClient(gpt))
                self.assertIn("Verified", notes.read_text(encoding="utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.assertGreaterEqual(len(requests), 3)
        self.assertTrue(all(item["path"] == "/responses" for item in requests))
        self.assertTrue(
            all(
                item["authorization"] == "Bearer ds-secret"
                for item in requests[:-1]
            )
        )
        self.assertEqual(requests[-1]["authorization"], "Bearer gpt-secret")
        self.assertTrue(all("API_KEY" not in json.dumps(item["payload"]) for item in requests))


class ProfileTests(unittest.TestCase):
    def test_profile_parser_reads_data_without_shell_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "must-not-exist"
            path = write_profile(root, "ds", api_key=f"$(touch-{marker.name})")
            profile = load_profile("ds", repo_root=root)
        self.assertEqual(profile.api_key, "$(touch-must-not-exist)")
        self.assertFalse(marker.exists())
        self.assertEqual(path.name, "ds.env")

    def test_profile_parser_rejects_unknown_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = write_profile(root, "ds")
            path.write_text(path.read_text(encoding="utf-8") + "COMMAND='bad'\n", encoding="utf-8")
            with self.assertRaisesRegex(ProfileError, "Unknown profile field"):
                load_profile("ds", repo_root=root)

    def test_profile_parser_rejects_broad_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = write_profile(root, "ds")
            os.chmod(path, 0o644)
            with self.assertRaisesRegex(ProfileError, "Unsafe permissions"):
                load_profile("ds", repo_root=root)

    def test_profile_fingerprint_excludes_name_and_key(self) -> None:
        profile = model_profile()
        changed = replace(profile, name="renamed", api_key="another-secret")
        self.assertEqual(profile.fingerprint(), changed.fingerprint())


class ResponsesAPITests(unittest.TestCase):
    def test_payload_uses_instructions_reasoning_and_json_mode(self) -> None:
        profile = model_profile()
        payload = response_payload(
            profile=profile,
            developer_text="system rules",
            user_text="course evidence",
            max_output_tokens=1234,
            structured_output=True,
        )
        self.assertEqual(payload["model"], "deepseek-flash")
        self.assertEqual(payload["instructions"], "system rules")
        self.assertEqual(payload["reasoning"], {"effort": "max"})
        self.assertEqual(payload["text"], {"format": {"type": "json_object"}})
        self.assertEqual(payload["max_output_tokens"], 1234)
        self.assertFalse(payload["store"])
        self.assertEqual(payload["input"][0]["role"], "user")
        self.assertNotIn("developer", json.dumps(payload))
        self.assertNotIn(profile.api_key, json.dumps(payload))

    def test_client_uses_selected_endpoint_and_key(self) -> None:
        profile = model_profile(api_key="secret-one")
        response = MagicMock()
        response.read.return_value = json.dumps(
            {"status": "completed", "output_text": "ok"}
        ).encode()
        context = MagicMock()
        context.__enter__.return_value = response
        context.__exit__.return_value = False
        with patch("urllib.request.urlopen", return_value=context) as urlopen:
            result = ResponsesClient(profile).create_response({"model": profile.model})
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.deepseek.com/responses")
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-one")
        self.assertEqual(result["output_text"], "ok")

    def test_incomplete_response_is_not_silently_parsed(self) -> None:
        client = ResponsesClient(model_profile())
        with patch.object(
            client,
            "request_json",
            return_value={
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
            },
        ):
            with self.assertRaisesRegex(ResponsesAPIError, "incomplete"):
                client.create_response({})

    def test_batch_is_rejected_before_network_for_deepseek(self) -> None:
        client = ResponsesClient(model_profile(batch_supported=False))
        with patch.object(client, "request_json") as request_json:
            with self.assertRaisesRegex(ResponsesAPIError, "does not support Batch"):
                client.create_batch("file-id")
        request_json.assert_not_called()

    def test_deepseek_image_is_normalized_to_local_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.jpg"
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=white:s=1280x720",
                    "-frames:v",
                    "1",
                    str(source),
                ],
                check=True,
            )
            payload = response_payload(
                profile=model_profile(),
                developer_text="rules",
                user_text="evidence",
                image_paths=[source],
            )
            data_url = payload["input"][0]["content"][1]["image_url"]
            encoded = base64.b64decode(data_url.split(",", 1)[1])
            normalized = Path(temporary) / "normalized.jpg"
            normalized.write_bytes(encoded)
            width, height = _image_dimensions(normalized)
        self.assertLessEqual(width * height, 640_000)
        self.assertLessEqual(len(encoded), 1_048_576)


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
