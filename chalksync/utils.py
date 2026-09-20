from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence, TypeVar
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


T = TypeVar("T")
MARKDOWN_TIMESTAMP_RE = re.compile(
    r"\[(?P<hours>\d{1,3}):(?P<minutes>\d{2}):(?P<seconds>\d{2})\]"
    r"(?:\((?P<url>https?://[^)\s]+)\))?"
)


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, value: Any) -> None:
    ensure_dir(path.parent)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    temporary.replace(path)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                rows.append(json.loads(stripped))
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    ensure_dir(path.parent)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")
    temporary.replace(path)


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    ensure_dir(path.parent)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
        handle.write("\n")


def format_timestamp(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def parse_timestamp(value: str) -> float:
    normalized = value.strip().replace(",", ".")
    parts = normalized.split(":")
    if len(parts) == 2:
        hours = 0
        minutes, seconds = parts
    elif len(parts) == 3:
        hours, minutes, seconds = parts
    else:
        raise ValueError(f"Unsupported timestamp: {value}")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def timestamp_url(base_url: str, seconds: int, *, query_parameter: str = "t") -> str:
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"Invalid web video URL: {base_url}")
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key != query_parameter]
    query.append((query_parameter, str(max(0, seconds))))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


def link_markdown_timestamps(
    markdown: str, base_url: str, *, query_parameter: str = "t"
) -> tuple[str, int]:
    timestamp_url(base_url, 0, query_parameter=query_parameter)
    count = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal count
        minutes = int(match.group("minutes"))
        seconds = int(match.group("seconds"))
        if minutes >= 60 or seconds >= 60:
            return match.group(0)
        total = int(match.group("hours")) * 3600 + minutes * 60 + seconds
        label = f"[{match.group('hours')}:{match.group('minutes')}:{match.group('seconds')}]"
        count += 1
        return f"{label}({timestamp_url(base_url, total, query_parameter=query_parameter)})"

    return MARKDOWN_TIMESTAMP_RE.sub(replace, markdown), count


def run(command: Sequence[str], *, capture: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def choose_evenly(items: Sequence[T], limit: int) -> list[T]:
    if limit <= 0 or not items:
        return []
    if len(items) <= limit:
        return list(items)
    if limit == 1:
        return [items[len(items) // 2]]
    indexes = [round(index * (len(items) - 1) / (limit - 1)) for index in range(limit)]
    return [items[index] for index in indexes]


def extract_json_object(text: str) -> dict[str, Any]:
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate, flags=re.IGNORECASE)
        candidate = re.sub(r"\s*```$", "", candidate)
    try:
        value = json.loads(candidate)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", candidate):
        try:
            value, _ = decoder.raw_decode(candidate[match.start() :])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("Model response did not contain a JSON object")


def model_from_argument(value: str | None, environment_name: str) -> str:
    model = value or os.environ.get(environment_name)
    if not model:
        raise ValueError(
            f"No model configured. Pass --model or set {environment_name}; "
            "the pipeline does not choose models automatically."
        )
    return model
