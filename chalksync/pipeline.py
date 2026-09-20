from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .media import extract_frames, link_video, locate_inbox_inputs, locate_video, probe_media
from .openai_api import ResponsesAPIError, ResponsesClient, response_payload, response_text
from .profiles import ModelProfile
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
LAYOUT_PROMPT_REVISION = 1
WORKER_PROMPT_REVISION = 1
FINAL_PROMPT_REVISION = 1


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _provenance(
    profile: ModelProfile,
    *,
    prompt_revision: int,
    input_fingerprint: str,
    operation: str,
) -> dict[str, Any]:
    cache_identity = {
        "profile_fingerprint": profile.fingerprint(operation),
        "prompt_revision": prompt_revision,
        "input_fingerprint": input_fingerprint,
        "operation": operation,
    }
    return {
        "cache_key": _fingerprint(cache_identity),
        "profile": profile.provenance(operation),
        "prompt_revision": prompt_revision,
        "input_fingerprint": input_fingerprint,
        "operation": operation,
    }


def _require_matching_provenance(
    actual: dict[str, Any] | None,
    expected: dict[str, Any],
    *,
    artifact: Path,
) -> None:
    if not actual or actual.get("cache_key") != expected["cache_key"]:
        raise RuntimeError(
            f"Cached artifact provenance does not match the selected profile or current inputs: {artifact}. "
            "Re-run this stage with --force to regenerate it."
        )


def _request_response(
    course_dir: Path,
    *,
    stage: str,
    client: ResponsesClient,
    payload: dict[str, Any],
) -> dict[str, Any]:
    try:
        return client.create_response(payload)
    except ResponsesAPIError as exc:
        operation = stage.split(":", 1)[0]
        if operation in {"final-section", "final-merge"}:
            operation = "final"
        write_json(
            course_dir / "state" / "last_error.json",
            {
                "occurred_at": datetime.now(timezone.utc).isoformat(),
                "stage": stage,
                "profile": client.profile.provenance(operation),
                "error_type": type(exc).__name__,
                "message": getattr(exc, "safe_message", "Responses request failed"),
            },
        )
        raise


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


def _usage(
    course_dir: Path, stage: str, profile: ModelProfile, response: dict[str, Any]
) -> None:
    operation = stage.split(":", 1)[0]
    if operation == "final-section" or operation == "final-merge":
        operation = "final"
    append_jsonl(
        course_dir / "state" / "usage.jsonl",
        {
            "stage": stage,
            "profile": profile.provenance(operation),
            "usage": response.get("usage", {}),
        },
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
    client: ResponsesClient,
    force: bool = False,
) -> dict[str, Any]:
    course_dir = course_dir.resolve()
    layout_path = course_dir / "visual" / "layout.json"
    config = load_course(course_dir)
    full_index = read_json(course_dir / "media" / "frames" / "full" / "index.json")
    sample_count = int(config["visual"]["layout_sample_frames"])
    samples = choose_evenly(full_index, sample_count)
    image_paths = [course_dir / item["path"] for item in samples]
    input_fingerprint = _fingerprint(
        {
            "samples": samples,
            "images": [_file_fingerprint(path) for path in image_paths],
            "image_detail": config["visual"]["image_detail"],
        }
    )
    provenance = _provenance(
        client.profile,
        prompt_revision=LAYOUT_PROMPT_REVISION,
        input_fingerprint=input_fingerprint,
        operation="layout",
    )
    if layout_path.is_file() and not force:
        existing = read_json(layout_path)
        _require_matching_provenance(
            existing.get("provenance"), provenance, artifact=layout_path
        )
        return existing
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
        profile=client.profile,
        developer_text=LAYOUT_DEVELOPER_PROMPT,
        user_text=user_text,
        image_paths=image_paths,
        image_detail=config["visual"]["image_detail"],
        max_output_tokens=client.profile.layout_max_output_tokens,
        structured_output=True,
    )
    response = _request_response(
        course_dir, stage="layout", client=client, payload=payload
    )
    layout = _validate_layout(extract_json_object(response_text(response)))
    layout["model"] = client.profile.model
    layout["sample_frames"] = samples
    layout["provenance"] = provenance
    write_json(layout_path, layout)
    _usage(course_dir, "layout", client.profile, response)
    return layout


def extract_region_frames(
    course_dir: Path, layout: dict[str, Any], *, force: bool = False
) -> list[dict[str, Any]]:
    course_dir = course_dir.resolve()
    index_path = course_dir / "visual" / "frames.json"
    manifest_path = course_dir / "visual" / "frames.manifest.json"
    config = load_course(course_dir)
    visual = config["visual"]
    extraction_identity = {
        "layout": _fingerprint(layout),
        "region_interval_seconds": visual["region_interval_seconds"],
        "board_scene_threshold": visual["board_scene_threshold"],
        "slides_scene_threshold": visual["slides_scene_threshold"],
        "max_scene_frames": visual["max_scene_frames"],
        "extraction_version": 1,
    }
    expected_fingerprint = _fingerprint(extraction_identity)
    if index_path.is_file() and not force:
        if not manifest_path.is_file():
            raise RuntimeError(
                f"Cached region frames have no provenance: {manifest_path}. "
                "Re-run the worker stage with --force."
            )
        manifest = read_json(manifest_path)
        if manifest.get("fingerprint") != expected_fingerprint:
            raise RuntimeError(
                f"Cached region frames do not match the current layout: {index_path}. "
                "Re-run the worker stage with --force."
            )
        return read_json(index_path)
    video = locate_video(course_dir)
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
    write_json(
        manifest_path,
        {"fingerprint": expected_fingerprint, "inputs": extraction_identity},
    )
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
    client: ResponsesClient,
    force: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    layout = detect_layout(course_dir, client=client, force=force)
    frames = extract_region_frames(course_dir, layout, force=force)
    return layout, frames


def _worker_payload(
    course_dir: Path,
    config: dict[str, Any],
    chunk: dict[str, Any],
    profile: ModelProfile,
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
        profile=profile,
        developer_text=WORKER_DEVELOPER_PROMPT,
        user_text=user_text,
        image_paths=[course_dir / frame["path"] for frame in chunk["frames"]],
        image_detail=config["visual"]["image_detail"],
        max_output_tokens=profile.worker_max_output_tokens,
        structured_output=True,
    )


def _time_label(seconds: float) -> str:
    from .utils import format_timestamp

    return format_timestamp(float(seconds))


def run_workers_sync(
    course_dir: Path,
    *,
    client: ResponsesClient,
    force: bool = False,
) -> list[Path]:
    course_dir = course_dir.resolve()
    config = load_course(course_dir)
    ensure_visual_assets(course_dir, client=client, force=force)
    chunks = build_chunks(course_dir)
    output_dir = ensure_dir(course_dir / "worker")
    outputs: list[Path] = []
    for chunk in chunks:
        output_path = output_dir / f"{chunk['id']}.json"
        outputs.append(output_path)
        input_fingerprint = _fingerprint(
            {
                "chunk": chunk,
                "images": [
                    _file_fingerprint(course_dir / frame["path"])
                    for frame in chunk["frames"]
                ],
            }
        )
        provenance = _provenance(
            client.profile,
            prompt_revision=WORKER_PROMPT_REVISION,
            input_fingerprint=input_fingerprint,
            operation="worker",
        )
        if output_path.is_file() and not force:
            existing = read_json(output_path)
            _require_matching_provenance(
                existing.get("provenance"), provenance, artifact=output_path
            )
            continue
        response = _request_response(
            course_dir,
            stage=f"worker:{chunk['id']}",
            client=client,
            payload=_worker_payload(course_dir, config, chunk, client.profile),
        )
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
                "model": client.profile.model,
                "provenance": provenance,
                "analysis": analysis,
            },
        )
        _usage(course_dir, f"worker:{chunk['id']}", client.profile, response)
    return outputs


def submit_worker_batch(
    course_dir: Path,
    *,
    client: ResponsesClient,
    force: bool = False,
) -> dict[str, Any]:
    client.require_batch()
    course_dir = course_dir.resolve()
    state_path = course_dir / "state" / "worker_batch.json"
    if state_path.is_file() and not force:
        existing = read_json(state_path)
        raise RuntimeError(
            f"A worker batch is already recorded ({existing.get('batch_id')}, "
            f"status {existing.get('status')}); collect it or pass --force to submit a replacement"
        )
    config = load_course(course_dir)
    ensure_visual_assets(course_dir, client=client, force=force)
    chunks = build_chunks(course_dir)
    request_fingerprint = _fingerprint(
        {
            "chunks": chunks,
            "images": [
                _file_fingerprint(course_dir / frame["path"])
                for chunk in chunks
                for frame in chunk["frames"]
            ],
        }
    )
    provenance = _provenance(
        client.profile,
        prompt_revision=WORKER_PROMPT_REVISION,
        input_fingerprint=request_fingerprint,
        operation="worker-batch",
    )
    input_path = course_dir / "state" / "worker_batch_input.jsonl"
    rows = [
        {
            "custom_id": chunk["id"],
            "method": "POST",
            "url": "/v1/responses",
            "body": _worker_payload(course_dir, config, chunk, client.profile),
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
        "model": client.profile.model,
        "profile_name": client.profile.name,
        "provenance": provenance,
        "chunks": len(chunks),
    }
    write_json(state_path, state)
    return state


def collect_worker_batch(course_dir: Path, *, client: ResponsesClient) -> dict[str, Any]:
    course_dir = course_dir.resolve()
    state_path = course_dir / "state" / "worker_batch.json"
    state = read_json(state_path)
    state_profile = (state.get("provenance") or {}).get("profile") or {}
    if state_profile.get("fingerprint") != client.profile.fingerprint("worker-batch"):
        raise RuntimeError(
            f"Batch state at {state_path} belongs to a different profile; "
            "collect it with the profile used to submit it."
        )
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
        input_fingerprint = _fingerprint(
            {
                "chunk": chunk,
                "images": [
                    _file_fingerprint(course_dir / frame["path"])
                    for frame in chunk["frames"]
                ],
            }
        )
        provenance = _provenance(
            client.profile,
            prompt_revision=WORKER_PROMPT_REVISION,
            input_fingerprint=input_fingerprint,
            operation="worker",
        )
        write_json(
            output_dir / f"{chunk_id}.json",
            {
                "chunk_id": chunk_id,
                "start": chunk["start"],
                "end": chunk["end"],
                "model": state["model"],
                "provenance": provenance,
                "analysis": analysis,
            },
        )
        _usage(course_dir, f"worker:{chunk_id}:batch", client.profile, response)
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
            "worker_provenance": worker.get("provenance"),
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
    client: ResponsesClient,
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
    bundles = _final_bundles(course_dir)
    groups = _group_bundles(bundles, max_source_characters)
    input_fingerprint = _fingerprint(
        {
            "title": config["title"],
            "output_language": config["output_language"],
            "bundles": bundles,
            "max_source_characters": max_source_characters,
        }
    )
    provenance = _provenance(
        client.profile,
        prompt_revision=FINAL_PROMPT_REVISION,
        input_fingerprint=input_fingerprint,
        operation="final",
    )
    if notes_path.is_file() and not force:
        manifest_path = course_dir / "notes" / "manifest.json"
        manifest = read_json(manifest_path) if manifest_path.is_file() else {}
        _require_matching_provenance(
            manifest.get("provenance"), provenance, artifact=notes_path
        )
        if configured_web_url:
            existing = notes_path.read_text(encoding="utf-8")
            linked, link_count = link_markdown_timestamps(
                existing,
                configured_web_url,
                query_parameter=timestamp_parameter,
            )
            notes_path.write_text(linked.rstrip() + "\n", encoding="utf-8")
            manifest.update(
                {
                    "web_video_url": configured_web_url,
                    "web_timestamp_parameter": timestamp_parameter,
                    "timestamp_links": link_count,
                }
            )
            write_json(manifest_path, manifest)
        return notes_path
    sections_dir = ensure_dir(course_dir / "notes" / "sections")
    if force:
        for section_path in sections_dir.glob("section-*.md"):
            section_path.unlink()
    if len(groups) == 1:
        user_text = (
            f"Course title: {config['title']}\n"
            f"Output language: {config['output_language']}\n"
            "Create the complete study guide from these evidence bundles:\n\n"
            + "\n\n".join(groups[0])
        )
        response = _request_response(
            course_dir,
            stage="final",
            client=client,
            payload=response_payload(
                profile=client.profile,
                developer_text=FINAL_DEVELOPER_PROMPT,
                user_text=user_text,
                max_output_tokens=client.profile.final_max_output_tokens,
            ),
        )
        markdown = response_text(response)
        _usage(course_dir, "final", client.profile, response)
    else:
        section_texts: list[str] = []
        for index, group in enumerate(groups, 1):
            user_text = (
                f"Course title: {config['title']}\n"
                f"Output language: {config['output_language']}\n"
                f"This is evidence group {index} of {len(groups)}.\n\n"
                + "\n\n".join(group)
            )
            response = _request_response(
                course_dir,
                stage=f"final-section:{index}",
                client=client,
                payload=response_payload(
                    profile=client.profile,
                    developer_text=SECTION_DEVELOPER_PROMPT,
                    user_text=user_text,
                    max_output_tokens=client.profile.final_max_output_tokens,
                ),
            )
            section = response_text(response)
            section_path = course_dir / "notes" / "sections" / f"section-{index:03d}.md"
            section_path.write_text(section.rstrip() + "\n", encoding="utf-8")
            section_texts.append(section)
            _usage(course_dir, f"final-section:{index}", client.profile, response)
        merge_text = (
            f"Course title: {config['title']}\n"
            f"Output language: {config['output_language']}\n\n"
            + "\n\n---\n\n".join(section_texts)
        )
        response = _request_response(
            course_dir,
            stage="final-merge",
            client=client,
            payload=response_payload(
                profile=client.profile,
                developer_text=MERGE_DEVELOPER_PROMPT,
                user_text=merge_text,
                max_output_tokens=client.profile.final_max_output_tokens,
            ),
        )
        markdown = response_text(response)
        _usage(course_dir, "final-merge", client.profile, response)
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
            "model": client.profile.model,
            "provenance": provenance,
            "groups": len(groups),
            "source_bundles": sum(len(group) for group in groups),
            "web_video_url": configured_web_url,
            "web_timestamp_parameter": timestamp_parameter,
            "timestamp_links": link_count,
        },
    )
    return notes_path
