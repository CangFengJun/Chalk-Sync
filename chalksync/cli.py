from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from .cleanup import cleanup_course
from .openai_api import OpenAIClient
from .pipeline import (
    collect_worker_batch,
    finalize_course,
    init_course,
    prepare_course,
    run_workers_sync,
    submit_worker_batch,
)
from .utils import model_from_argument
from .viewer import build_viewer, serve_course


def _course(value: str) -> Path:
    return Path(value).expanduser().resolve()


def _client(args: argparse.Namespace) -> OpenAIClient:
    return OpenAIClient(base_url=getattr(args, "api_base", None))


def _add_api_base(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--api-base", help="API base URL; defaults to OPENAI_BASE_URL or the OpenAI API")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chalksync")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("doctor", help="Check local prerequisites")

    init = subparsers.add_parser("init", help="Create a course inbox and configuration")
    init.add_argument("course_dir")
    init.add_argument("--title", required=True)
    init.add_argument("--language", default="zh-CN")
    init.add_argument("--web-video-url", help="Public video page used for timestamp links")

    prepare = subparsers.add_parser("prepare", help="Validate inbox, import subtitles, and extract full frames")
    prepare.add_argument("course_dir")
    prepare.add_argument("--force", action="store_true")

    worker = subparsers.add_parser("worker", help="Detect board/slides and analyze timestamped chunks")
    worker.add_argument("course_dir")
    worker.add_argument("--model", help="Worker model; or set CHALKSYNC_WORKER_MODEL")
    worker.add_argument("--mode", choices=["sync", "batch-submit"], default="sync")
    worker.add_argument("--force", action="store_true")
    _add_api_base(worker)

    collect = subparsers.add_parser("collect", help="Collect a submitted worker Batch job")
    collect.add_argument("course_dir")
    _add_api_base(collect)

    finalize = subparsers.add_parser("finalize", help="Verify evidence and write final course notes")
    finalize.add_argument("course_dir")
    finalize.add_argument("--model", help="Final model; or set CHALKSYNC_FINAL_MODEL")
    finalize.add_argument("--web-video-url", help="Public video page used for timestamp links")
    finalize.add_argument("--max-source-characters", type=int, default=120_000)
    finalize.add_argument("--force", action="store_true")
    _add_api_base(finalize)

    viewer = subparsers.add_parser("viewer", help="Build the synchronized local viewer")
    viewer.add_argument("course_dir")

    serve = subparsers.add_parser("serve", help="Serve the synchronized local viewer")
    serve.add_argument("course_dir")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--open", action="store_true", dest="open_browser")

    cleanup = subparsers.add_parser("cleanup", help="Remove generated intermediates after final notes are verified")
    cleanup.add_argument("course_dir")
    cleanup.add_argument("--delete-video", action="store_true", help="Also permanently delete the inbox video")
    cleanup.add_argument("--delete-subtitle", action="store_true", help="Also permanently delete the inbox Markdown subtitle")
    cleanup.add_argument("--dry-run", action="store_true", help="List targets and estimated bytes without deleting")

    run = subparsers.add_parser("run", help="Run prepare, synchronous workers, finalization, and viewer build")
    run.add_argument("course_dir")
    run.add_argument("--worker-model", help="Worker model; or set CHALKSYNC_WORKER_MODEL")
    run.add_argument("--final-model", help="Final model; or set CHALKSYNC_FINAL_MODEL")
    run.add_argument("--web-video-url", help="Public video page used for timestamp links")
    run.add_argument("--cleanup", action="store_true", help="Delete generated intermediates after finalization")
    run.add_argument("--delete-video", action="store_true", help="With --cleanup, permanently delete the inbox video")
    run.add_argument("--delete-subtitle", action="store_true", help="With --cleanup, permanently delete the inbox subtitle")
    run.add_argument("--force", action="store_true")
    _add_api_base(run)
    return parser


def doctor() -> int:
    checks = {
        "python": sys.executable,
        "ffmpeg": shutil.which("ffmpeg"),
        "ffprobe": shutil.which("ffprobe"),
        "OPENAI_API_KEY": "configured" if os.environ.get("OPENAI_API_KEY") else None,
    }
    for name, value in checks.items():
        print(f"{name:16} {'OK: ' + value if value else 'MISSING'}")
    return 0 if all(checks.values()) else 1


def _print_cleanup(report: dict) -> None:
    action = "Would reclaim" if report["dry_run"] else "Reclaimed"
    print(f"{action}: {report['bytes_reclaimed']} bytes")
    for target in report["targets"]:
        print(f"  - {target}")
    print(f"Preserved notes: {report['notes']} ({report['timestamp_links']} web timestamp links)")


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "doctor":
            raise SystemExit(doctor())
        if args.command == "init":
            course_dir = _course(args.course_dir)
            init_course(
                course_dir,
                title=args.title,
                language=args.language,
                web_video_url=args.web_video_url,
            )
            print(f"Course initialized: {course_dir}")
            print(f"Place exactly one video and one timestamp-prefixed Markdown subtitle in: {course_dir / 'inbox'}")
            return
        if args.command == "prepare":
            config = prepare_course(_course(args.course_dir), force=args.force)
            print(
                f"Prepared {config['title']}: {config['transcript']['segments']} subtitle segments, "
                f"{config['visual']['full_frames']} full frames"
            )
            return
        if args.command == "worker":
            course_dir = _course(args.course_dir)
            model = model_from_argument(args.model, "CHALKSYNC_WORKER_MODEL")
            if args.mode == "sync":
                outputs = run_workers_sync(
                    course_dir, client=_client(args), model=model, force=args.force
                )
                print(f"Worker stage complete: {len(outputs)} chunks")
            else:
                state = submit_worker_batch(
                    course_dir,
                    client=_client(args),
                    model=model,
                    force=args.force,
                )
                print(f"Batch submitted: {state['batch_id']} ({state['status']})")
            return
        if args.command == "collect":
            state = collect_worker_batch(_course(args.course_dir), client=_client(args))
            print(f"Batch status: {state['status']}")
            return
        if args.command == "finalize":
            model = model_from_argument(args.model, "CHALKSYNC_FINAL_MODEL")
            path = finalize_course(
                _course(args.course_dir),
                client=_client(args),
                model=model,
                web_video_url=args.web_video_url,
                max_source_characters=args.max_source_characters,
                force=args.force,
            )
            print(f"Notes written: {path}")
            return
        if args.command == "viewer":
            print(f"Viewer written: {build_viewer(_course(args.course_dir))}")
            return
        if args.command == "serve":
            serve_course(
                _course(args.course_dir),
                host=args.host,
                port=args.port,
                open_browser=args.open_browser,
            )
            return
        if args.command == "cleanup":
            report = cleanup_course(
                _course(args.course_dir),
                delete_video=args.delete_video,
                delete_subtitle=args.delete_subtitle,
                dry_run=args.dry_run,
            )
            _print_cleanup(report)
            return
        if args.command == "run":
            if (args.delete_video or args.delete_subtitle) and not args.cleanup:
                raise ValueError("--delete-video and --delete-subtitle require --cleanup")
            course_dir = _course(args.course_dir)
            worker_model = model_from_argument(args.worker_model, "CHALKSYNC_WORKER_MODEL")
            final_model = model_from_argument(args.final_model, "CHALKSYNC_FINAL_MODEL")
            client = _client(args)
            prepare_course(course_dir, force=args.force)
            run_workers_sync(
                course_dir,
                client=client,
                model=worker_model,
                force=args.force,
            )
            finalize_course(
                course_dir,
                client=client,
                model=final_model,
                web_video_url=args.web_video_url,
                force=args.force,
            )
            if args.cleanup:
                report = cleanup_course(
                    course_dir,
                    delete_video=args.delete_video,
                    delete_subtitle=args.delete_subtitle,
                )
                _print_cleanup(report)
            else:
                print(f"Viewer written: {build_viewer(course_dir)}")
            return
    except (FileNotFoundError, FileExistsError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
