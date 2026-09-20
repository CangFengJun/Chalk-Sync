from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .media import VIDEO_SUFFIXES
from .pipeline import load_course
from .utils import MARKDOWN_TIMESTAMP_RE, read_json, timestamp_url, write_json


GENERATED_DIRECTORIES = ("media", "transcript", "visual", "chunks", "worker", "state", "viewer")


def _path_size(path: Path) -> int:
    if path.is_symlink() or path.is_file():
        return path.lstat().st_size
    if not path.is_dir():
        return 0
    return sum(item.lstat().st_size for item in path.rglob("*") if item.is_file() or item.is_symlink())


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def cleanup_course(
    course_dir: Path,
    *,
    delete_video: bool = False,
    delete_subtitle: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    course_dir = course_dir.resolve()
    config = load_course(course_dir)
    notes_path = course_dir / "notes" / "course.md"
    manifest_path = course_dir / "notes" / "manifest.json"
    if not notes_path.is_file() or notes_path.stat().st_size == 0:
        raise RuntimeError("Final notes do not exist; cleanup is allowed only after successful finalization")

    manifest = read_json(manifest_path) if manifest_path.is_file() else {}
    notes = notes_path.read_text(encoding="utf-8")
    configured_url = config.get("web_video_url")
    timestamp_parameter = str(config.get("web_timestamp_parameter") or "t")
    linked_timestamps = []
    for match in MARKDOWN_TIMESTAMP_RE.finditer(notes):
        if not match.group("url") or not configured_url:
            continue
        total = (
            int(match.group("hours")) * 3600
            + int(match.group("minutes")) * 60
            + int(match.group("seconds"))
        )
        expected = timestamp_url(configured_url, total, query_parameter=timestamp_parameter)
        if match.group("url") == expected:
            linked_timestamps.append(match)
    if delete_video and (not configured_url or not linked_timestamps):
        raise RuntimeError(
            "Refusing to delete the input video because the final notes do not contain "
            "verified web timestamp links"
        )

    targets = [course_dir / name for name in GENERATED_DIRECTORIES]
    targets.append(course_dir / "notes" / "sections")
    inbox = course_dir / "inbox"
    if delete_video and inbox.is_dir():
        targets.extend(
            path for path in inbox.iterdir() if path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES
        )
    if delete_subtitle and inbox.is_dir():
        targets.extend(path for path in inbox.iterdir() if path.is_file() and path.suffix.lower() == ".md")

    existing = [path for path in targets if path.exists() or path.is_symlink()]
    deleted = [str(path.relative_to(course_dir)) for path in existing]
    bytes_reclaimed = sum(_path_size(path) for path in existing)
    report = {
        "dry_run": dry_run,
        "delete_video": delete_video,
        "delete_subtitle": delete_subtitle,
        "targets": deleted,
        "bytes_reclaimed": bytes_reclaimed,
        "notes": str(notes_path.relative_to(course_dir)),
        "web_video_url": configured_url,
        "timestamp_links": len(linked_timestamps),
    }
    if dry_run:
        return report

    for path in existing:
        _remove(path)
    config["cleanup"] = {
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "deleted_video": delete_video,
        "deleted_subtitle": delete_subtitle,
        "bytes_reclaimed": bytes_reclaimed,
        "preserved_notes": str(notes_path.relative_to(course_dir)),
        "web_timestamp_links": len(linked_timestamps),
    }
    write_json(course_dir / "course.json", config)
    manifest["cleanup"] = config["cleanup"]
    write_json(manifest_path, manifest)
    return report
