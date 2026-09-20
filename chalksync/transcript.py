from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable

from .utils import format_timestamp, parse_timestamp, write_jsonl


MARKDOWN_LINE_RE = re.compile(
    r"^\s*(?:[-*+]\s+)?(?:#{1,6}\s*)?"
    r"(?P<start>\d{1,2}:\d{2}(?::\d{2})?(?:[,.]\d{1,3})?)"
    r"(?P<text>.*)$"
)


def parse_markdown_subtitle(path: Path, *, video_duration: float | None = None) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        match = MARKDOWN_LINE_RE.match(line)
        if not match:
            continue
        text = match.group("text").strip()
        text = re.sub(r"^\s*(?:[-–—:：|>]+)\s*", "", text)
        text = text.rstrip("|").strip()
        if not text:
            continue
        entries.append(
            {
                "start": round(parse_timestamp(match.group("start")), 3),
                "text": text,
                "source_line": line_number,
            }
        )
    if not entries:
        raise ValueError(
            f"No timestamp-prefixed subtitle lines found in {path}; "
            "expected lines such as '00:06也就是你和AI沟通的那个渠道'"
        )
    for previous, current in zip(entries, entries[1:]):
        if current["start"] < previous["start"]:
            raise ValueError(
                f"Subtitle timestamps must be non-decreasing; line {current['source_line']} "
                f"comes before line {previous['source_line']}"
            )
    if video_duration is not None and entries[-1]["start"] > video_duration + 1:
        raise ValueError(
            f"Last subtitle starts at {format_timestamp(entries[-1]['start'])}, "
            f"after video duration {format_timestamp(video_duration)}"
        )
    for index, entry in enumerate(entries):
        if index + 1 < len(entries):
            end = entries[index + 1]["start"]
        elif video_duration is not None and video_duration > entry["start"]:
            end = video_duration
        else:
            end = entry["start"] + 8
        entry["end"] = round(max(entry["start"], end), 3)
    return entries


def import_transcript(
    source: Path, destination: Path, *, video_duration: float | None = None
) -> list[dict[str, Any]]:
    suffix = source.suffix.lower()
    if suffix == ".md":
        segments = parse_markdown_subtitle(source, video_duration=video_duration)
    else:
        raise ValueError("Transcript must be a timestamp-prefixed Markdown file")
    normalized = normalize_segments(segments)
    if not normalized:
        raise ValueError(f"No subtitle segments found in {source}")
    write_jsonl(destination, normalized)
    return normalized


def normalize_segments(segments: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in segments:
        text = re.sub(r"\s+", " ", str(item.get("text", ""))).strip()
        if not text:
            continue
        start = max(0.0, float(item.get("start", 0)))
        end = max(start, float(item.get("end", start)))
        normalized.append({"start": round(start, 3), "end": round(end, 3), "text": text})
    normalized.sort(key=lambda item: (item["start"], item["end"]))
    return normalized


def render_segments(segments: Iterable[dict[str, Any]]) -> str:
    return "\n".join(f"[{format_timestamp(item['start'])}] {item['text']}" for item in segments)


def chunk_segments(
    segments: list[dict[str, Any]], duration: float, chunk_seconds: int
) -> list[dict[str, Any]]:
    if chunk_seconds <= 0:
        raise ValueError("chunk_seconds must be positive")
    effective_duration = max(duration, max((item["end"] for item in segments), default=0))
    count = max(1, int((effective_duration + chunk_seconds - 1) // chunk_seconds))
    chunks: list[dict[str, Any]] = []
    for index in range(count):
        start = index * chunk_seconds
        end = min(effective_duration, (index + 1) * chunk_seconds)
        selected = [
            item
            for item in segments
            if ((item["start"] + item["end"]) / 2) >= start
            and ((item["start"] + item["end"]) / 2) < end
        ]
        chunks.append(
            {
                "id": f"chunk-{index + 1:04d}",
                "index": index,
                "start": round(start, 3),
                "end": round(end, 3),
                "start_label": format_timestamp(start),
                "end_label": format_timestamp(end),
                "segments": selected,
                "transcript": render_segments(selected),
            }
        )
    return chunks
