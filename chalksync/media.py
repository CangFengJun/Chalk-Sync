from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .utils import choose_evenly, ensure_dir, run, write_json


VIDEO_SUFFIXES = {".mp4", ".mkv", ".webm", ".mov", ".m4v"}


def require_program(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"Required program '{name}' was not found on PATH")
    return path


def locate_video(course_dir: Path) -> Path:
    media_dir = course_dir / "media"
    candidates = sorted(
        path
        for path in media_dir.glob("source.*")
        if path.suffix.lower() in VIDEO_SUFFIXES and path.is_file()
    )
    if not candidates:
        raise FileNotFoundError(f"No prepared video found under {media_dir}; run prepare first")
    return candidates[0]


def link_video(course_dir: Path, source: Path, *, force: bool = False) -> Path:
    source = source.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if source.suffix.lower() not in VIDEO_SUFFIXES:
        raise ValueError(f"Unsupported video extension: {source.suffix}")
    media_dir = ensure_dir(course_dir / "media")
    destination = media_dir / f"source{source.suffix.lower()}"
    existing_sources = [
        path
        for path in media_dir.glob("source.*")
        if path.suffix.lower() in VIDEO_SUFFIXES and (path.exists() or path.is_symlink())
    ]
    for existing in existing_sources:
        try:
            if existing.resolve() == source:
                return existing
        except FileNotFoundError:
            pass
    if existing_sources:
        if not force:
            raise FileExistsError(
                f"A different prepared video already exists: {existing_sources[0]}; pass --force to replace it"
            )
        for existing in existing_sources:
            if not existing.is_symlink():
                raise FileExistsError(f"Refusing to replace non-symlink media file: {existing}")
            existing.unlink()
    if destination.exists() or destination.is_symlink():
        if not force:
            raise FileExistsError(f"{destination} already exists; pass --force to replace it")
        destination.unlink()
    destination.symlink_to(source)
    return destination


def probe_media(path: Path) -> dict[str, Any]:
    ffprobe = require_program("ffprobe")
    completed = run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(path),
        ]
    )
    payload = json.loads(completed.stdout)
    video_stream = next(
        (stream for stream in payload.get("streams", []) if stream.get("codec_type") == "video"),
        {},
    )
    format_info = payload.get("format", {})
    return {
        "path": str(path.resolve()),
        "duration": float(format_info.get("duration") or video_stream.get("duration") or 0),
        "size": int(format_info.get("size") or 0),
        "format_name": format_info.get("format_name"),
        "width": int(video_stream.get("width") or 0),
        "height": int(video_stream.get("height") or 0),
        "codec": video_stream.get("codec_name"),
    }


def _remove_generated_frames(directory: Path) -> None:
    for pattern in ("periodic_*.jpg", "scene_*.jpg"):
        for path in directory.glob(pattern):
            path.unlink()


def _scene_timestamps(stderr: str) -> list[float]:
    timestamps: list[float] = []
    pattern = re.compile(r"pts_time:(-?\d+(?:\.\d+)?)")
    for line in stderr.splitlines():
        if "showinfo" not in line or "pts_time:" not in line:
            continue
        match = pattern.search(line)
        if match:
            timestamps.append(float(match.group(1)))
    return timestamps


def extract_frames(
    video: Path,
    output_dir: Path,
    *,
    interval_seconds: int = 45,
    scene_threshold: float = 0.18,
    max_scene_frames: int = 240,
    force: bool = False,
    crop: dict[str, float] | None = None,
    relative_root: Path | None = None,
) -> list[dict[str, Any]]:
    ffmpeg = require_program("ffmpeg")
    ensure_dir(output_dir)
    index_path = output_dir / "index.json"
    if index_path.is_file() and not force:
        return json.loads(index_path.read_text(encoding="utf-8"))
    _remove_generated_frames(output_dir)

    filters: list[str] = []
    if crop:
        filters.append(
            "crop="
            f"iw*{crop['width']}:ih*{crop['height']}:"
            f"iw*{crop['x']}:ih*{crop['y']}"
        )
    filters.append("scale=1280:-2:force_original_aspect_ratio=decrease")
    base_filter = ",".join(filters)
    periodic_command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video),
        "-vf",
        f"fps=1/{interval_seconds},{base_filter}",
        "-q:v",
        "4",
        str(output_dir / "periodic_%06d.jpg"),
    ]
    run(periodic_command)
    periodic_files = sorted(output_dir.glob("periodic_*.jpg"))
    periodic = [
        {"time": float(index * interval_seconds), "path": str(path), "kind": "periodic"}
        for index, path in enumerate(periodic_files)
    ]

    scene_filter = f"{base_filter},select=gt(scene\\,{scene_threshold}),showinfo"
    scene_command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "info",
        "-y",
        "-i",
        str(video),
        "-vf",
        scene_filter,
        "-fps_mode",
        "vfr",
        "-q:v",
        "4",
        str(output_dir / "scene_%06d.jpg"),
    ]
    completed = subprocess.run(
        scene_command,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    scene_files = sorted(output_dir.glob("scene_*.jpg"))
    timestamps = _scene_timestamps(completed.stderr)
    scene = [
        {"time": timestamp, "path": str(path), "kind": "scene"}
        for path, timestamp in zip(scene_files, timestamps)
    ]
    if len(scene) > max_scene_frames:
        kept = choose_evenly(scene, max_scene_frames)
        kept_paths = {item["path"] for item in kept}
        for path in scene_files:
            if str(path) not in kept_paths:
                path.unlink()
        scene = kept

    combined = sorted(periodic + scene, key=lambda item: (item["time"], item["kind"] != "scene"))
    deduplicated: list[dict[str, Any]] = []
    root = (relative_root or output_dir.parent.parent).resolve()
    for item in combined:
        item["path"] = str(Path(item["path"]).resolve().relative_to(root))
        if deduplicated and abs(item["time"] - deduplicated[-1]["time"]) < 2:
            if item["kind"] == "scene":
                deduplicated[-1] = item
            continue
        item["time"] = round(float(item["time"]), 3)
        deduplicated.append(item)
    write_json(index_path, deduplicated)
    return deduplicated


def locate_inbox_inputs(course_dir: Path) -> tuple[Path, Path]:
    inbox = course_dir / "inbox"
    if not inbox.is_dir():
        raise FileNotFoundError(f"Missing course inbox: {inbox}; run init first")
    videos = sorted(path for path in inbox.iterdir() if path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES)
    subtitles = sorted(path for path in inbox.iterdir() if path.is_file() and path.suffix.lower() == ".md")
    if len(videos) != 1 or len(subtitles) != 1:
        raise ValueError(
            f"{inbox} must contain exactly one video and one timestamp-prefixed Markdown subtitle file; "
            f"found {len(videos)} video(s) and {len(subtitles)} subtitle file(s)"
        )
    return videos[0], subtitles[0]
