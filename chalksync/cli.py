from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from .cleanup import cleanup_course
from .openai_api import ResponsesClient
from .pipeline import (
    collect_worker_batch,
    finalize_course,
    init_course,
    prepare_course,
    run_workers_sync,
    submit_worker_batch,
)
from .profiles import PROFILE_NAMES, ProfileError, load_profile, profile_path, repository_root
from .utils import read_json
from .viewer import build_viewer, serve_course


def _course(value: str) -> Path:
    return Path(value).expanduser().resolve()


def _client(profile_name: str) -> ResponsesClient:
    return ResponsesClient(load_profile(profile_name))


def _print_progress(message: str) -> None:
    print(message, flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chalksync-profile")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor_parser = subparsers.add_parser("doctor", help="Check tools and both local profiles")
    doctor_parser.add_argument(
        "--probe",
        action="store_true",
        help="Authenticate against each profile's models endpoint without generating content",
    )

    init = subparsers.add_parser("init", help="Create a course inbox and configuration")
    init.add_argument("course_dir")
    init.add_argument("--title", required=True)
    init.add_argument("--language", default="zh-CN")
    init.add_argument("--web-video-url", help="Public video page used for timestamp links")

    prepare = subparsers.add_parser(
        "prepare", help="Validate inbox, import subtitles, and extract full frames"
    )
    prepare.add_argument("course_dir")
    prepare.add_argument("--force", action="store_true")

    worker = subparsers.add_parser(
        "worker", help="Detect board/slides and analyze timestamped chunks"
    )
    worker.add_argument("course_dir")
    worker.add_argument("--profile", choices=PROFILE_NAMES, default="ds")
    worker.add_argument("--mode", choices=["sync", "batch-submit"], default="sync")
    worker.add_argument("--force", action="store_true")

    collect = subparsers.add_parser("collect", help="Collect a submitted worker Batch job")
    collect.add_argument("course_dir")
    collect.add_argument("--profile", choices=PROFILE_NAMES)

    finalize = subparsers.add_parser(
        "finalize", help="Verify evidence and write final course notes"
    )
    finalize.add_argument("course_dir")
    finalize.add_argument("--profile", choices=PROFILE_NAMES, default="gpt")
    finalize.add_argument("--web-video-url", help="Public video page used for timestamp links")
    finalize.add_argument("--max-source-characters", type=int, default=120_000)
    finalize.add_argument("--force", action="store_true")

    viewer = subparsers.add_parser("viewer", help="Build the synchronized local viewer")
    viewer.add_argument("course_dir")

    serve = subparsers.add_parser("serve", help="Serve the synchronized local viewer")
    serve.add_argument("course_dir")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--open", action="store_true", dest="open_browser")

    cleanup = subparsers.add_parser(
        "cleanup", help="Remove generated intermediates after final notes are verified"
    )
    cleanup.add_argument("course_dir")
    cleanup.add_argument(
        "--delete-video", action="store_true", help="Also permanently delete the inbox video"
    )
    cleanup.add_argument(
        "--delete-subtitle",
        action="store_true",
        help="Also permanently delete the inbox Markdown subtitle",
    )
    cleanup.add_argument("--dry-run", action="store_true")

    run = subparsers.add_parser(
        "run", help="Run prepare, synchronous workers, finalization, and viewer build"
    )
    run.add_argument("course_dir")
    run.add_argument("--worker", choices=PROFILE_NAMES, default="ds")
    run.add_argument("--final", choices=PROFILE_NAMES, default="gpt")
    run.add_argument("--web-video-url", help="Public video page used for timestamp links")
    run.add_argument("--max-source-characters", type=int, default=120_000)
    run.add_argument("--cleanup", action="store_true")
    run.add_argument(
        "--delete-video", action="store_true", help="With --cleanup, permanently delete the inbox video"
    )
    run.add_argument(
        "--delete-subtitle",
        action="store_true",
        help="With --cleanup, permanently delete the inbox subtitle",
    )
    run.add_argument("--force", action="store_true")
    return parser


def _git_ignores(path: Path, repo_root: Path) -> bool:
    completed = subprocess.run(
        ["git", "check-ignore", "--quiet", str(path)],
        cwd=repo_root,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return completed.returncode == 0


def doctor(*, probe: bool = False) -> int:
    root = repository_root()
    checks: list[tuple[str, bool, str]] = [
        ("python", True, sys.executable),
        ("ffmpeg", bool(shutil.which("ffmpeg")), shutil.which("ffmpeg") or "missing"),
        ("ffprobe", bool(shutil.which("ffprobe")), shutil.which("ffprobe") or "missing"),
    ]
    for name in PROFILE_NAMES:
        path = profile_path(name, root)
        ignored = path.exists() and _git_ignores(path, root)
        checks.append((f"{name} git-ignore", ignored, str(path)))
        try:
            profile = load_profile(name, repo_root=root)
        except ProfileError as exc:
            checks.append((f"{name} profile", False, str(exc)))
            continue
        summary = f"{profile.model} @ {profile.base_url} ({profile.reasoning_effort})"
        checks.append((f"{name} profile", True, summary))
        if probe:
            try:
                ResponsesClient(profile).probe()
            except RuntimeError as exc:
                checks.append((f"{name} probe", False, str(exc)))
            else:
                checks.append((f"{name} probe", True, "authenticated"))
    for name, ok, detail in checks:
        print(f"{name:18} {'OK' if ok else 'MISSING'}: {detail}")
    return 0 if all(ok for _, ok, _ in checks) else 1


def _print_cleanup(report: dict) -> None:
    action = "Would reclaim" if report["dry_run"] else "Reclaimed"
    print(f"{action}: {report['bytes_reclaimed']} bytes")
    for target in report["targets"]:
        print(f"  - {target}")
    print(f"Preserved notes: {report['notes']} ({report['timestamp_links']} web timestamp links)")


def _batch_profile(course_dir: Path, selected: str | None) -> str:
    if selected:
        return selected
    state_path = course_dir / "state" / "worker_batch.json"
    state = read_json(state_path)
    name = state.get("profile_name")
    if name not in PROFILE_NAMES:
        raise ValueError(f"Batch state does not name a valid profile: {state_path}")
    return str(name)


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "doctor":
            raise SystemExit(doctor(probe=args.probe))
        if args.command == "init":
            course_dir = _course(args.course_dir)
            init_course(
                course_dir,
                title=args.title,
                language=args.language,
                web_video_url=args.web_video_url,
            )
            print(f"Course initialized: {course_dir}")
            print(
                "Place exactly one video and one timestamp-prefixed Markdown subtitle in: "
                f"{course_dir / 'inbox'}"
            )
            return
        if args.command == "prepare":
            config = prepare_course(
                _course(args.course_dir), force=args.force, progress=_print_progress
            )
            print(
                f"Prepared {config['title']}: {config['transcript']['segments']} subtitle segments, "
                f"{config['visual']['full_frames']} full frames"
            )
            return
        if args.command == "worker":
            course_dir = _course(args.course_dir)
            client = _client(args.profile)
            if args.mode == "sync":
                outputs = run_workers_sync(
                    course_dir, client=client, force=args.force, progress=_print_progress
                )
                print(f"Worker stage complete: {len(outputs)} chunks")
            else:
                state = submit_worker_batch(
                    course_dir, client=client, force=args.force, progress=_print_progress
                )
                print(f"Batch submitted: {state['batch_id']} ({state['status']})")
            return
        if args.command == "collect":
            course_dir = _course(args.course_dir)
            profile_name = _batch_profile(course_dir, args.profile)
            state = collect_worker_batch(course_dir, client=_client(profile_name))
            print(f"Batch status: {state['status']}")
            return
        if args.command == "finalize":
            path = finalize_course(
                _course(args.course_dir),
                client=_client(args.profile),
                web_video_url=args.web_video_url,
                max_source_characters=args.max_source_characters,
                force=args.force,
                progress=_print_progress,
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
            worker_client = _client(args.worker)
            final_client = _client(args.final)
            prepare_course(course_dir, force=args.force, progress=_print_progress)
            run_workers_sync(
                course_dir, client=worker_client, force=args.force, progress=_print_progress
            )
            finalize_course(
                course_dir,
                client=final_client,
                web_video_url=args.web_video_url,
                max_source_characters=args.max_source_characters,
                force=args.force,
                progress=_print_progress,
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
