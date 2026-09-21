from __future__ import annotations

import base64
import http.client
import json
import math
import mimetypes
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from .profiles import ModelProfile


class ResponsesAPIError(RuntimeError):
    def __init__(self, message: str, *, safe_message: str | None = None):
        super().__init__(message)
        self.safe_message = safe_message or message


class ResponsesClient:
    def __init__(self, profile: ModelProfile):
        self.profile = profile

    def _headers(self, content_type: str | None = "application/json") -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.profile.api_key}"}
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        content_type: str | None = "application/json",
        retries: int = 3,
    ) -> bytes:
        url = f"{self.profile.base_url}/{path.lstrip('/')}"
        headers = self._headers(content_type)
        if method.upper() == "POST":
            headers["Idempotency-Key"] = uuid.uuid4().hex
        request = urllib.request.Request(
            url,
            data=body,
            headers=headers,
            method=method,
        )
        for attempt in range(retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=180) as response:
                    return response.read()
            except urllib.error.HTTPError as exc:
                exc.read()
                if exc.code in {408, 409, 429, 500, 502, 503, 504, 524} and attempt < retries:
                    time.sleep(2**attempt)
                    continue
                raise ResponsesAPIError(
                    f"{self.profile.name} Responses endpoint returned HTTP {exc.code}",
                    safe_message=f"Responses endpoint returned HTTP {exc.code}",
                ) from exc
            except (
                urllib.error.URLError,
                http.client.RemoteDisconnected,
                ConnectionError,
                TimeoutError,
            ) as exc:
                if attempt < retries:
                    time.sleep(2**attempt)
                    continue
                raise ResponsesAPIError(
                    f"{self.profile.name} Responses endpoint request failed: {exc}",
                    safe_message="Responses endpoint request failed",
                ) from exc
        raise AssertionError("unreachable")

    def request_json(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        raw = self._request(method, path, body=body)
        return json.loads(raw.decode("utf-8"))

    def create_response(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.request_json("POST", "/responses", payload)
        status = response.get("status")
        if status in {"failed", "incomplete", "cancelled"}:
            details = response.get("incomplete_details") or response.get("error") or {}
            reason = details.get("reason") if isinstance(details, dict) else None
            suffix = f" ({reason})" if reason in {"max_output_tokens", "content_filter"} else ""
            raise ResponsesAPIError(
                f"{self.profile.name} response ended with status {status}{suffix}",
                safe_message=f"Response ended with status {status}{suffix}",
            )
        return response

    def probe(self) -> None:
        self.request_json("GET", "/models")

    def require_batch(self) -> None:
        if not self.profile.batch_supported:
            raise ResponsesAPIError(
                f"Profile {self.profile.name!r} does not support Batch; use worker --mode sync"
            )

    def upload_batch_file(self, path: Path) -> dict[str, Any]:
        self.require_batch()
        body, content_type = _multipart_body([("purpose", "batch")], "file", path)
        raw = self._request("POST", "/files", body=body, content_type=content_type)
        return json.loads(raw.decode("utf-8"))

    def create_batch(self, input_file_id: str) -> dict[str, Any]:
        self.require_batch()
        return self.request_json(
            "POST",
            "/batches",
            {
                "input_file_id": input_file_id,
                "endpoint": "/v1/responses",
                "completion_window": "24h",
                "metadata": {"pipeline": "chalksync", "stage": "worker"},
            },
        )

    def get_batch(self, batch_id: str) -> dict[str, Any]:
        self.require_batch()
        return self.request_json("GET", f"/batches/{batch_id}")

    def get_file_content(self, file_id: str) -> bytes:
        self.require_batch()
        return self._request("GET", f"/files/{file_id}/content", content_type=None)


def _multipart_body(
    fields: list[tuple[str, str]], file_field: str, file_path: Path
) -> tuple[bytes, str]:
    boundary = f"----chalksync-{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields:
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                value.encode("utf-8"),
                b"\r\n",
            ]
        )
    mime_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    chunks.extend(
        [
            f"--{boundary}\r\n".encode(),
            (
                f'Content-Disposition: form-data; name="{file_field}"; '
                f'filename="{file_path.name}"\r\n'
            ).encode(),
            f"Content-Type: {mime_type}\r\n\r\n".encode(),
            file_path.read_bytes(),
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _image_dimensions(path: Path) -> tuple[int, int]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise ResponsesAPIError("ffprobe is required to enforce the selected profile's image policy")
    completed = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=s=x:p=0",
            str(path),
        ],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        width, height = (int(value) for value in completed.stdout.strip().split("x", 1))
    except (TypeError, ValueError) as exc:
        raise ResponsesAPIError(f"Could not read image dimensions for {path}") from exc
    if width <= 0 or height <= 0:
        raise ResponsesAPIError(f"Invalid image dimensions for {path}: {width}x{height}")
    return width, height


def _encode_normalized_image(path: Path, profile: ModelProfile) -> tuple[bytes, str]:
    max_pixels = profile.image_max_pixels
    max_bytes = profile.image_max_bytes
    raw = path.read_bytes()
    mime_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    if max_pixels is None or max_bytes is None:
        return raw, mime_type

    width, height = _image_dimensions(path)
    if width * height <= max_pixels and len(raw) <= max_bytes:
        return raw, mime_type

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ResponsesAPIError("ffmpeg is required to enforce the selected profile's image policy")
    scale = min(1.0, math.sqrt(max_pixels / (width * height)))
    target_width = max(2, int(width * scale) // 2 * 2)
    target_height = max(2, int(height * scale) // 2 * 2)

    with tempfile.TemporaryDirectory(prefix="chalksync-image-") as temporary:
        output = Path(temporary) / "normalized.jpg"
        for _ in range(10):
            for quality in (3, 5, 8, 12, 18, 24, 31):
                subprocess.run(
                    [
                        ffmpeg,
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-y",
                        "-i",
                        str(path),
                        "-vf",
                        f"scale={target_width}:{target_height}:force_original_aspect_ratio=decrease",
                        "-q:v",
                        str(quality),
                        str(output),
                    ],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                encoded = output.read_bytes()
                if len(encoded) <= max_bytes:
                    normalized_width, normalized_height = _image_dimensions(output)
                    if normalized_width * normalized_height <= max_pixels:
                        return encoded, "image/jpeg"
            if target_width <= 2 or target_height <= 2:
                break
            target_width = max(2, int(target_width * 0.8) // 2 * 2)
            target_height = max(2, int(target_height * 0.8) // 2 * 2)
    raise ResponsesAPIError(
        f"Could not normalize {path} below {max_pixels} pixels and {max_bytes} bytes"
    )


def image_part(profile: ModelProfile, path: Path, detail: str = "low") -> dict[str, Any]:
    raw, mime_type = _encode_normalized_image(path, profile)
    encoded = base64.b64encode(raw).decode("ascii")
    return {
        "type": "input_image",
        "image_url": f"data:{mime_type};base64,{encoded}",
        "detail": detail,
    }


def response_payload(
    *,
    profile: ModelProfile,
    developer_text: str,
    user_text: str,
    image_paths: list[Path] | None = None,
    image_detail: str = "low",
    max_output_tokens: int = 5000,
    structured_output: bool = False,
) -> dict[str, Any]:
    content: list[dict[str, Any]] = [{"type": "input_text", "text": user_text}]
    for path in image_paths or []:
        content.append(image_part(profile, path, image_detail))
    payload: dict[str, Any] = {
        "model": profile.model,
        "store": False,
        "instructions": developer_text,
        "reasoning": {"effort": profile.reasoning_effort},
        "input": [{"role": "user", "content": content}],
        "max_output_tokens": max_output_tokens,
    }
    if structured_output:
        payload["text"] = {"format": {"type": "json_object"}}
    return payload


def response_text(response: dict[str, Any]) -> str:
    direct = response.get("output_text")
    if isinstance(direct, str) and direct:
        return direct
    texts: list[str] = []
    for item in response.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"} and content.get("text"):
                texts.append(str(content["text"]))
    if not texts:
        raise ResponsesAPIError("Response contained no output text")
    return "\n".join(texts)
