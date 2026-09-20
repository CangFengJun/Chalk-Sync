from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .media import extract_frames, link_video, locate_inbox_inputs, locate_video, probe_media
from .openai_api import OpenAIClient, response_payload, response_text
from .prompts import (
    FINAL_DEVELOPER_PROMPT,
    LAYOUT_DEVELOPER_PROMPT,
    MERGE_DEVELOPER_PROMPT,
    SECTION_DEVELOPER_PROMPT,
    WORKER_DEVELOPER_PROMPT,
)
from .transcript import chunk_segments, import_transcript
from .utils import (
    append_jsonl,
    choose_evenly,
    ensure_dir,
    extract_json_object,
    link_markdown_timestamps,
    read_json,
    read_jsonl,
    write_json,
    write_jsonl,
    timestamp_url,
)


COURSE_FILE = "course.json"


def init_course(
    course_dir: Path,
    *,
    title: str,
    language: str = "zh-CN",
    web_video_url: str | None = None,
) -> dict[str, Any]:
    course_dir = course_dir.resolve()
    ensure_dir(course_dir / "inbox")
    config_path = course_dir / COURSE_FILE
    if config_path.exists():
        return read_json(config_path)
    config = {
        "schema_version": 1,
        "title": title,
        "language": language,
        "output_language": language,
        "web_video_url": web_video_url,
        "web_timestamp_parameter": "t",
        "chunk_seconds": 300,
        "visual": {
            "layout_sample_frames": 6,
            "full_frame_interval_seconds": 60,
            "region_interval_seconds": 30,
            "full_scene_threshold": 0.18,
            "board_scene_threshold": 0.025,
            "slides_scene_threshold": 0.08,
            "max_scene_frames": 240,
            "max_frames_per_chunk": 6,
            "image_detail": "high",
        },
    }
    write_json(config_path, config)
    return config


def load_course(course_dir: Path) -> dict[str, Any]:
    path = course_dir.resolve() / COURSE_FILE
    if not path.is_file():
        raise FileNotFoundError(f"Missing {path}; run init first")
    return read_json(path)


def _save_course(course_dir: Path, config: dict[str, Any]) -> None:
    write_json(course_dir.resolve() / COURSE_FILE, config)


def prepare_course(course_dir: Path, *, force: bool = False) -> dict[str, Any]:
    course_dir = course_dir.resolve()
    config = load_course(course_dir)
    inbox_video, inbox_subtitle = locate_inbox_inputs(course_dir)
    video = link_video(course_dir, inbox_video, force=force)
    media_info = probe_media(video)
    transcript_path = course_dir / "transcript" / "segments.jsonl"
    segments = import_transcript(
        inbox_subtitle,
        transcript_path,
        video_duration=media_info["duration"],
    )

    visual = config["visual"]
    full_frames = extract_frames(
        video,
        course_dir / "media" / "frames" / "full",
        interval_seconds=int(visual["full_frame_interval_seconds"]),
        scene_threshold=float(visual["full_scene_threshold"]),
        max_scene_frames=int(visual["max_scene_frames"]),
        force=force,
        relative_root=course_dir,
    )
    config["media"] = media_info
    config["media"]["course_video"] = str(video.relative_to(course_dir))
    config["transcript"] = {
        "source": str(inbox_subtitle.relative_to(course_dir)),
        "segments": len(segments),
        "path": str(transcript_path.relative_to(course_dir)),
    }
    config["visual"]["full_frames"] = len(full_frames)
    _save_course(course_dir, config)
    return config


def _usage(course_dir: Path, stage: str, model: str, response: dict[str, Any]) -> None:
    append_jsonl(
        course_dir / "state" / "usage.jsonl",
        {"stage": stage, "model": model, "usage": response.get("usage", {})},
    )


def _validate_layout(value: dict[str, Any]) -> dict[str, Any]:
    regions = value.get("regions")
    if not isinstance(regions, list) or not regions:
        raise ValueError("Layout response has no regions")
    validated: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(regions[:4]):
        if not isinstance(item, dict):
            continue
        region_id = re.sub(r"[^a-z0-9-]+", "-", str(item.get("id") or f"region-{index + 1}").lower()).strip("-")
        if not region_id or region_id in seen:
            region_id = f"region-{index + 1}"
        seen.add(region_id)
        kind = str(item.get("kind", "other")).lower()
        if kind not in {"board", "slides", "screen", "other"}:
            kind = "other"
        x = max(0.0, min(0.95, float(item.get("x", 0))))
        y = max(0.0, min(0.95, float(item.get("y", 0))))
        width = max(0.05, min(1.0 - x, float(item.get("width", 1 - x))))
        height = max(0.05, min(1.0 - y, float(item.get("height", 1 - y))))
        validated.append(
            {
                "id": region_id,
                "kind": kind,
                "label": str(item.get("label") or kind),
                "x": round(x, 4),
                "y": round(y, 4),
                "width": round(width, 4),
                "height": round(height, 4),
                "confidence": round(max(0.0, min(1.0, float(item.get("confidence", 0)))), 3),
                "notes": str(item.get("notes", "")),
            }
        )
    if not validated:
        raise ValueError("Layout response contained no valid regions")
    return {"regions": validated, "uncertainties": value.get("uncertainties", [])}


def detect_layout(
    course_dir: Path,
    *,
    client: OpenAIClient,
    model: str,
    force: bool = False,
) -> dict[str, Any]:
    course_dir = course_dir.resolve()
    layout_path = course_dir / "visual" / "layout.json"
    if layout_path.is_file() and not force:
        return read_json(layout_path)
    config = load_course(course_dir)
    full_index = read_json(course_dir / "media" / "frames" / "full" / "index.json")
    sample_count = int(config["visual"]["layout_sample_frames"])
    samples = choose_evenly(full_index, sample_count)
    image_paths = [course_dir / item["path"] for item in samples]
    labels = "\n".join(
        f"Image {index + 1}: video time {item['time']:.3f}s" for index, item in enumerate(samples)
    )
    user_text = (
        f"Course: {config['title']}\n"
        f"Representative frame labels:\n{labels}\n"
        "Identify stable board and projected-slide/screen teaching regions. "
        "Use a full-frame region only when a meaningful split cannot be identified."
    )
    payload = response_payload(
        model=model,
        developer_text=LAYOUT_DEVELOPER_PROMPT,
        user_text=user_text,
        image_paths=image_paths,
        image_detail=config["visual"]["image_detail"],
        max_output_tokens=2500,
    )
    response = client.create_response(payload)
    layout = _validate_layout(extract_json_object(response_text(response)))
    layout["model"] = model
    layout["sample_frames"] = samples
    write_json(layout_path, layout)
    _usage(course_dir, "layout", model, response)
    return layout


def extract_region_frames(
    course_dir: Path, layout: dict[str, Any], *, force: bool = False
) -> list[dict[str, Any]]:
    course_dir = course_dir.resolve()
    index_path = course_dir / "visual" / "frames.json"
    if index_path.is_file() and not force:
        return read_json(index_path)
    config = load_course(course_dir)
    video = locate_video(course_dir)
    visual = config["visual"]
    combined: list[dict[str, Any]] = []
    for region in layout["regions"]:
        threshold_key = "board_scene_threshold" if region["kind"] == "board" else "slides_scene_threshold"
        frames = extract_frames(
            video,
            course_dir / "media" / "frames" / "regions" / region["id"],
            interval_seconds=int(visual["region_interval_seconds"]),
            scene_threshold=float(visual[threshold_key]),
            max_scene_frames=int(visual["max_scene_frames"]),
            force=force,
            crop=region,
            relative_root=course_dir,
        )
        for frame in frames:
            combined.append(
                {
                    **frame,
                    "region_id": region["id"],
                    "region_kind": region["kind"],
                    "region_label": region["label"],
                }
            )
    combined.sort(key=lambda item: (item["time"], item["region_id"], item["kind"]))
    write_json(index_path, combined)
    return combined


def _select_chunk_frames(
    frames: list[dict[str, Any]], start: float, end: float, limit: int
) -> list[dict[str, Any]]:
    candidates = [item for item in frames if start <= float(item["time"]) < end]
    if len(candidates) <= limit:
        return candidates
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in candidates:
        grouped.setdefault(item["region_kind"], []).append(item)
    selected: list[dict[str, Any]] = []
    quota = max(1, limit // max(1, len(grouped)))
    for kind in sorted(grouped):
        selected.extend(choose_evenly(grouped[kind], quota))
    selected_ids = {item["path"] for item in selected}
    remaining = [item for item in candidates if item["path"] not in selected_ids]
    selected.extend(choose_evenly(remaining, max(0, limit - len(selected))))
    return sorted(selected[:limit], key=lambda item: (item["time"], item["region_kind"]))


def build_chunks(course_dir: Path) -> list[dict[str, Any]]:
    course_dir = course_dir.resolve()
    config = load_course(course_dir)
    segments = read_jsonl(course_dir / "transcript" / "segments.jsonl")
    frames = read_json(course_dir / "visual" / "frames.json")
    chunks = chunk_segments(segments, float(config["media"]["duration"]), int(config["chunk_seconds"]))
    limit = int(config["visual"]["max_frames_per_chunk"])
    for chunk in chunks:
        chunk["frames"] = _select_chunk_frames(frames, chunk["start"], chunk["end"], limit)
    write_json(course_dir / "chunks" / "chunks.json", chunks)
    return chunks


def ensure_visual_assets(
    course_dir: Path,
    *,
    client: OpenAIClient,
    model: str,
    force: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    layout = detect_layout(course_dir, client=client, model=model, force=force)
    frames = extract_region_frames(course_dir, layout, force=force)
    return layout, frames


def _worker_payload(
    course_dir: Path, config: dict[str, Any], chunk: dict[str, Any], model: str
) -> dict[str, Any]:
    frame_labels = "\n".join(
        f"Image {index + 1}: [{_time_label(frame['time'])}] "
        f"{frame['region_kind']} region ({frame['region_label']})"
        for index, frame in enumerate(chunk["frames"])
    )
    user_text = (
        f"Course: {config['title']}\n"
        f"Output language: {config['output_language']}\n"
        f"Chunk: {chunk['start_label']} to {chunk['end_label']}\n"
        f"Frame labels:\n{frame_labels or '(no frames in this chunk)'}\n\n"
        f"Timestamped transcript:\n{chunk['transcript'] or '(no speech transcript in this chunk)'}"
    )
    return response_payload(
        model=model,
        developer_text=WORKER_DEVELOPER_PROMPT,
        user_text=user_text,
        image_paths=[course_dir / frame["path"] for frame in chunk["frames"]],
        image_detail=config["visual"]["image_detail"],
        max_output_tokens=5000,
    )


def _time_label(seconds: float) -> str:
    from .utils import format_timestamp

    return format_timestamp(float(seconds))


def run_workers_sync(
    course_dir: Path,
    *,
    client: OpenAIClient,
    model: str,
    force: bool = False,
) -> list[Path]:
    course_dir = course_dir.resolve()
    config = load_course(course_dir)
    ensure_visual_assets(course_dir, client=client, model=model, force=force)
    chunks = build_chunks(course_dir)
    output_dir = ensure_dir(course_dir / "worker")
    outputs: list[Path] = []
    for chunk in chunks:
        output_path = output_dir / f"{chunk['id']}.json"
        outputs.append(output_path)
        if output_path.is_file() and not force:
            continue
        response = client.create_response(_worker_payload(course_dir, config, chunk, model))
        raw = response_text(response)
        try:
            analysis = extract_json_object(raw)
        except ValueError:
            raw_path = output_dir / f"{chunk['id']}.raw.txt"
            raw_path.write_text(raw, encoding="utf-8")
            raise ValueError(f"Invalid worker JSON for {chunk['id']}; raw output saved to {raw_path}")
        write_json(
            output_path,
            {
                "chunk_id": chunk["id"],
                "start": chunk["start"],
                "end": chunk["end"],
                "model": model,
                "analysis": analysis,
            },
        )
        _usage(course_dir, f"worker:{chunk['id']}", model, response)
    return outputs


def submit_worker_batch(
    course_dir: Path,
    *,
    client: OpenAIClient,
    model: str,
    force: bool = False,
) -> dict[str, Any]:
    course_dir = course_dir.resolve()
    state_path = course_dir / "state" / "worker_batch.json"
    if state_path.is_file() and not force:
        existing = read_json(state_path)
        raise RuntimeError(
            f"A worker batch is already recorded ({existing.get('batch_id')}, "
            f"status {existing.get('status')}); collect it or pass --force to submit a replacement"
        )
    config = load_course(course_dir)
    ensure_visual_assets(course_dir, client=client, model=model, force=force)
    chunks = build_chunks(course_dir)
    input_path = course_dir / "state" / "worker_batch_input.jsonl"
    rows = [
        {
            "custom_id": chunk["id"],
            "method": "POST",
            "url": "/v1/responses",
            "body": _worker_payload(course_dir, config, chunk, model),
        }
        for chunk in chunks
    ]
    write_jsonl(input_path, rows)
    uploaded = client.upload_batch_file(input_path)
    batch = client.create_batch(uploaded["id"])
    state = {
        "batch_id": batch["id"],
        "input_file_id": uploaded["id"],
        "status": batch.get("status"),
        "model": model,
        "chunks": len(chunks),
    }
    write_json(state_path, state)
    return state


def collect_worker_batch(course_dir: Path, *, client: OpenAIClient) -> dict[str, Any]:
    course_dir = course_dir.resolve()
    state_path = course_dir / "state" / "worker_batch.json"
    state = read_json(state_path)
    if state.get("collected"):
        return state
    batch = client.get_batch(state["batch_id"])
    state["status"] = batch.get("status")
    state["request_counts"] = batch.get("request_counts")
    state["errors"] = batch.get("errors")
    write_json(state_path, state)
    if batch.get("status") != "completed":
        return state
    output_file_id = batch.get("output_file_id")
    if not output_file_id:
        raise RuntimeError("Completed batch has no output_file_id")
    raw = client.get_file_content(output_file_id)
    result_path = course_dir / "state" / "worker_batch_output.jsonl"
    ensure_dir(result_path.parent)
    result_path.write_bytes(raw)
    chunks = {chunk["id"]: chunk for chunk in read_json(course_dir / "chunks" / "chunks.json")}
    output_dir = ensure_dir(course_dir / "worker")
    failures: list[str] = []
    for line in raw.decode("utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        chunk_id = item.get("custom_id")
        response_wrapper = item.get("response") or {}
        if item.get("error") or int(response_wrapper.get("status_code", 500)) >= 300:
            failures.append(str(chunk_id))
            continue
        response = response_wrapper.get("body") or {}
        analysis = extract_json_object(response_text(response))
        chunk = chunks[chunk_id]
        write_json(
            output_dir / f"{chunk_id}.json",
            {
                "chunk_id": chunk_id,
                "start": chunk["start"],
                "end": chunk["end"],
                "model": state["model"],
                "analysis": analysis,
            },
        )
        _usage(course_dir, f"worker:{chunk_id}:batch", state["model"], response)
    if failures:
        raise RuntimeError(f"Batch completed with failed chunks: {', '.join(failures)}")
    state["collected"] = True
    state["output_file_id"] = output_file_id
    write_json(state_path, state)
    return state


def _final_bundles(course_dir: Path) -> list[str]:
    chunks = read_json(course_dir / "chunks" / "chunks.json")
    bundles: list[str] = []
    missing: list[str] = []
    for chunk in chunks:
        worker_path = course_dir / "worker" / f"{chunk['id']}.json"
        if not worker_path.is_file():
            missing.append(chunk["id"])
            continue
        worker = read_json(worker_path)
        bundle = {
            "chunk_id": chunk["id"],
            "range": [chunk["start_label"], chunk["end_label"]],
            "transcript": chunk["transcript"],
            "worker_analysis": worker["analysis"],
        }
        bundles.append(json.dumps(bundle, ensure_ascii=False, indent=2))
    if missing:
        raise RuntimeError(f"Missing worker results for: {', '.join(missing)}")
    return bundles


def _group_bundles(bundles: list[str], character_limit: int) -> list[list[str]]:
    groups: list[list[str]] = []
    current: list[str] = []
    size = 0
    for bundle in bundles:
        if current and size + len(bundle) > character_limit:
            groups.append(current)
            current = []
            size = 0
        current.append(bundle)
        size += len(bundle)
    if current:
        groups.append(current)
    return groups


def finalize_course(
    course_dir: Path,
    *,
    client: OpenAIClient,
    model: str,
    web_video_url: str | None = None,
    max_source_characters: int = 120_000,
    force: bool = False,
) -> Path:
    course_dir = course_dir.resolve()
    notes_path = course_dir / "notes" / "course.md"
    config = load_course(course_dir)
    if web_video_url:
        timestamp_url(web_video_url, 0)
        config["web_video_url"] = web_video_url
        _save_course(course_dir, config)
    configured_web_url = config.get("web_video_url")
    timestamp_parameter = str(config.get("web_timestamp_parameter") or "t")
    if notes_path.is_file() and not force:
        if configured_web_url:
            existing = notes_path.read_text(encoding="utf-8")
            linked, link_count = link_markdown_timestamps(
                existing,
                configured_web_url,
                query_parameter=timestamp_parameter,
            )
            notes_path.write_text(linked.rstrip() + "\n", encoding="utf-8")
            manifest_path = course_dir / "notes" / "manifest.json"
            manifest = read_json(manifest_path) if manifest_path.is_file() else {}
            manifest.update(
                {
                    "web_video_url": configured_web_url,
                    "web_timestamp_parameter": timestamp_parameter,
                    "timestamp_links": link_count,
                }
            )
            write_json(manifest_path, manifest)
        return notes_path
    groups = _group_bundles(_final_bundles(course_dir), max_source_characters)
    ensure_dir(course_dir / "notes" / "sections")
    if len(groups) == 1:
        user_text = (
            f"Course title: {config['title']}\n"
            f"Output language: {config['output_language']}\n"
            "Create the complete study guide from these evidence bundles:\n\n"
            + "\n\n".join(groups[0])
        )
        response = client.create_response(
            response_payload(
                model=model,
                developer_text=FINAL_DEVELOPER_PROMPT,
                user_text=user_text,
                max_output_tokens=20_000,
            )
        )
        markdown = response_text(response)
        _usage(course_dir, "final", model, response)
    else:
        section_texts: list[str] = []
        for index, group in enumerate(groups, 1):
            user_text = (
                f"Course title: {config['title']}\n"
                f"Output language: {config['output_language']}\n"
                f"This is evidence group {index} of {len(groups)}.\n\n"
                + "\n\n".join(group)
            )
            response = client.create_response(
                response_payload(
                    model=model,
                    developer_text=SECTION_DEVELOPER_PROMPT,
                    user_text=user_text,
                    max_output_tokens=12_000,
                )
            )
            section = response_text(response)
            section_path = course_dir / "notes" / "sections" / f"section-{index:03d}.md"
            section_path.write_text(section.rstrip() + "\n", encoding="utf-8")
            section_texts.append(section)
            _usage(course_dir, f"final-section:{index}", model, response)
        merge_text = (
            f"Course title: {config['title']}\n"
            f"Output language: {config['output_language']}\n\n"
            + "\n\n---\n\n".join(section_texts)
        )
        response = client.create_response(
            response_payload(
                model=model,
                developer_text=MERGE_DEVELOPER_PROMPT,
                user_text=merge_text,
                max_output_tokens=20_000,
            )
        )
        markdown = response_text(response)
        _usage(course_dir, "final-merge", model, response)
    link_count = 0
    if configured_web_url:
        markdown, link_count = link_markdown_timestamps(
            markdown,
            configured_web_url,
            query_parameter=timestamp_parameter,
        )
    ensure_dir(notes_path.parent)
    notes_path.write_text(markdown.rstrip() + "\n", encoding="utf-8")
    write_json(
        course_dir / "notes" / "manifest.json",
        {
            "model": model,
            "groups": len(groups),
            "source_bundles": sum(len(group) for group in groups),
            "web_video_url": configured_web_url,
            "web_timestamp_parameter": timestamp_parameter,
            "timestamp_links": link_count,
        },
    )
    return notes_path
