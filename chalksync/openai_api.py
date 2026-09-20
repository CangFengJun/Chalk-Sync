from __future__ import annotations

import base64
import json
import mimetypes
import os
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any


class OpenAIAPIError(RuntimeError):
    pass


class OpenAIClient:
    def __init__(self, *, api_key: str | None = None, base_url: str | None = None):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")

    def _headers(self, content_type: str | None = "application/json") -> dict[str, str]:
        if not self.api_key:
            raise OpenAIAPIError("OPENAI_API_KEY is not set")
        headers = {"Authorization": f"Bearer {self.api_key}"}
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
        url = f"{self.base_url}/{path.lstrip('/')}"
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
                detail = exc.read().decode("utf-8", errors="replace")[:4000]
                if exc.code in {408, 409, 429, 500, 502, 503, 504} and attempt < retries:
                    time.sleep(2**attempt)
                    continue
                raise OpenAIAPIError(f"OpenAI API returned HTTP {exc.code}: {detail}") from exc
            except urllib.error.URLError as exc:
                if attempt < retries:
                    time.sleep(2**attempt)
                    continue
                raise OpenAIAPIError(f"OpenAI API request failed: {exc}") from exc
        raise AssertionError("unreachable")

    def request_json(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        raw = self._request(method, path, body=body)
        return json.loads(raw.decode("utf-8"))

    def create_response(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request_json("POST", "/responses", payload)

    def upload_batch_file(self, path: Path) -> dict[str, Any]:
        body, content_type = _multipart_body([("purpose", "batch")], "file", path)
        raw = self._request("POST", "/files", body=body, content_type=content_type)
        return json.loads(raw.decode("utf-8"))

    def create_batch(self, input_file_id: str) -> dict[str, Any]:
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
        return self.request_json("GET", f"/batches/{batch_id}")

    def get_file_content(self, file_id: str) -> bytes:
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


def image_part(path: Path, detail: str = "low") -> dict[str, Any]:
    mime_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return {
        "type": "input_image",
        "image_url": f"data:{mime_type};base64,{encoded}",
        "detail": detail,
    }


def response_payload(
    *,
    model: str,
    developer_text: str,
    user_text: str,
    image_paths: list[Path] | None = None,
    image_detail: str = "low",
    max_output_tokens: int = 5000,
) -> dict[str, Any]:
    content: list[dict[str, Any]] = [{"type": "input_text", "text": user_text}]
    for path in image_paths or []:
        content.append(image_part(path, image_detail))
    return {
        "model": model,
        "store": False,
        "input": [
            {
                "role": "developer",
                "content": [{"type": "input_text", "text": developer_text}],
            },
            {"role": "user", "content": content},
        ],
        "max_output_tokens": max_output_tokens,
    }


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
        raise OpenAIAPIError("Response contained no output text")
    return "\n".join(texts)
