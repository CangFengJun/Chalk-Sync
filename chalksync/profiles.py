from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


PROFILE_NAMES = ("ds", "gpt")
PROFILE_FIELDS = {
    "PROVIDER",
    "API_KEY",
    "BASE_URL",
    "WIRE_API",
    "MODEL",
    "REASONING_EFFORT",
    "BATCH_SUPPORTED",
    "IMAGE_MAX_PIXELS",
    "IMAGE_MAX_BYTES",
    "LAYOUT_MAX_OUTPUT_TOKENS",
    "WORKER_MAX_OUTPUT_TOKENS",
    "FINAL_MAX_OUTPUT_TOKENS",
}
REQUIRED_FIELDS = PROFILE_FIELDS - {"IMAGE_MAX_PIXELS", "IMAGE_MAX_BYTES"}
PROFILE_LINE_RE = re.compile(r"^(?P<key>[A-Z][A-Z0-9_]*)=(?P<value>.*)$")
EFFORTS = {
    "deepseek": {"none", "low", "high", "max"},
    "gpt": {"none", "minimal", "low", "medium", "high", "xhigh"},
}


class ProfileError(ValueError):
    pass


@dataclass(frozen=True)
class ModelProfile:
    name: str
    provider: str
    api_key: str
    base_url: str
    wire_api: str
    model: str
    reasoning_effort: str
    batch_supported: bool
    image_max_pixels: int | None
    image_max_bytes: int | None
    layout_max_output_tokens: int
    worker_max_output_tokens: int
    final_max_output_tokens: int

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "provider": self.provider,
            "base_url": self.base_url,
            "wire_api": self.wire_api,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "batch_supported": self.batch_supported,
            "image_policy": {
                "max_pixels": self.image_max_pixels,
                "max_bytes": self.image_max_bytes,
                "version": 1,
            },
            "output_tokens": {
                "layout": self.layout_max_output_tokens,
                "worker": self.worker_max_output_tokens,
                "final": self.final_max_output_tokens,
            },
        }

    def request_identity(self, operation: str = "all") -> dict[str, Any]:
        identity: dict[str, Any] = {
            "provider": self.provider,
            "base_url": self.base_url,
            "wire_api": self.wire_api,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
        }
        if operation in {"layout", "worker", "worker-batch", "all"}:
            identity["image_policy"] = self.public()["image_policy"]
        if operation == "layout":
            identity["max_output_tokens"] = self.layout_max_output_tokens
        elif operation in {"worker", "worker-batch"}:
            identity["max_output_tokens"] = self.worker_max_output_tokens
        elif operation == "final":
            identity["max_output_tokens"] = self.final_max_output_tokens
        else:
            identity["output_tokens"] = self.public()["output_tokens"]
        if operation in {"worker-batch", "all"}:
            identity["batch_supported"] = self.batch_supported
        return identity

    def fingerprint(self, operation: str = "all") -> str:
        encoded = json.dumps(
            self.request_identity(operation), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def provenance(self, operation: str = "all") -> dict[str, Any]:
        return {"fingerprint": self.fingerprint(operation), **self.public()}


def repository_root() -> Path:
    return Path(__file__).resolve().parent.parent


def profile_directory(repo_root: Path | None = None) -> Path:
    return (repo_root or repository_root()) / ".local-secrets" / "profiles"


def profile_path(name: str, repo_root: Path | None = None) -> Path:
    if name not in PROFILE_NAMES:
        raise ProfileError(f"Unknown profile {name!r}; choose one of: {', '.join(PROFILE_NAMES)}")
    return profile_directory(repo_root) / f"{name}.env"


def _check_private_path(path: Path, expected_mode: int, kind: str) -> None:
    if path.is_symlink():
        raise ProfileError(f"Refusing symlinked {kind}: {path}")
    try:
        info = path.stat()
    except FileNotFoundError as exc:
        raise ProfileError(f"Missing {kind}: {path}") from exc
    actual_mode = stat.S_IMODE(info.st_mode)
    if actual_mode != expected_mode:
        raise ProfileError(
            f"Unsafe permissions on {path}: expected {expected_mode:o}, found {actual_mode:o}; "
            f"run chmod {expected_mode:o} {path}"
        )
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise ProfileError(f"{kind.capitalize()} is not owned by the current user: {path}")


def _unquote(raw: str, path: Path, line_number: int) -> str:
    value = raw.strip()
    if not value:
        return ""
    if value[0] in {"'", '"'}:
        if len(value) < 2 or value[-1] != value[0]:
            raise ProfileError(f"Unclosed quote at {path}:{line_number}")
        return value[1:-1]
    if any(character.isspace() for character in value):
        raise ProfileError(f"Unquoted whitespace at {path}:{line_number}")
    return value


def _read_fields(path: Path) -> dict[str, str]:
    fields: dict[str, str] = {}
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            match = PROFILE_LINE_RE.fullmatch(stripped)
            if not match:
                raise ProfileError(f"Invalid profile line at {path}:{line_number}; expected KEY=VALUE")
            key = match.group("key")
            if key not in PROFILE_FIELDS:
                raise ProfileError(f"Unknown profile field {key!r} at {path}:{line_number}")
            if key in fields:
                raise ProfileError(f"Duplicate profile field {key!r} at {path}:{line_number}")
            fields[key] = _unquote(match.group("value"), path, line_number)
    missing = sorted(REQUIRED_FIELDS - fields.keys())
    if missing:
        raise ProfileError(f"Missing fields in {path}: {', '.join(missing)}")
    return fields


def _positive_int(fields: dict[str, str], key: str, path: Path, *, optional: bool = False) -> int | None:
    raw = fields.get(key, "")
    if optional and not raw:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise ProfileError(f"{key} must be a positive integer in {path}") from exc
    if value <= 0:
        raise ProfileError(f"{key} must be a positive integer in {path}")
    return value


def _boolean(fields: dict[str, str], key: str, path: Path) -> bool:
    raw = fields[key].lower()
    if raw not in {"true", "false"}:
        raise ProfileError(f"{key} must be true or false in {path}")
    return raw == "true"


def load_profile(name: str, *, repo_root: Path | None = None) -> ModelProfile:
    path = profile_path(name, repo_root)
    secrets_dir = path.parent.parent
    profiles_dir = path.parent
    _check_private_path(secrets_dir, 0o700, "secrets directory")
    _check_private_path(profiles_dir, 0o700, "profiles directory")
    _check_private_path(path, 0o600, "profile file")
    fields = _read_fields(path)

    provider = fields["PROVIDER"].lower()
    if provider not in EFFORTS:
        raise ProfileError(f"PROVIDER must be deepseek or gpt in {path}")
    if fields["WIRE_API"] != "responses":
        raise ProfileError(f"WIRE_API must be responses in {path}")
    effort = fields["REASONING_EFFORT"].lower()
    if effort not in EFFORTS[provider]:
        allowed = ", ".join(sorted(EFFORTS[provider]))
        raise ProfileError(f"REASONING_EFFORT for {provider} must be one of {allowed} in {path}")
    if not fields["API_KEY"]:
        raise ProfileError(f"API_KEY is empty in {path}")
    if not fields["MODEL"]:
        raise ProfileError(f"MODEL is empty in {path}")

    base_url = fields["BASE_URL"].rstrip("/")
    parsed = urlsplit(base_url)
    is_loopback_http = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if not parsed.netloc or (parsed.scheme != "https" and not is_loopback_http):
        raise ProfileError(f"BASE_URL must use HTTPS, except for a loopback HTTP endpoint, in {path}")

    image_max_pixels = _positive_int(fields, "IMAGE_MAX_PIXELS", path, optional=True)
    image_max_bytes = _positive_int(fields, "IMAGE_MAX_BYTES", path, optional=True)
    if (image_max_pixels is None) != (image_max_bytes is None):
        raise ProfileError(f"IMAGE_MAX_PIXELS and IMAGE_MAX_BYTES must both be set or both be empty in {path}")

    return ModelProfile(
        name=name,
        provider=provider,
        api_key=fields["API_KEY"],
        base_url=base_url,
        wire_api=fields["WIRE_API"],
        model=fields["MODEL"],
        reasoning_effort=effort,
        batch_supported=_boolean(fields, "BATCH_SUPPORTED", path),
        image_max_pixels=image_max_pixels,
        image_max_bytes=image_max_bytes,
        layout_max_output_tokens=int(_positive_int(fields, "LAYOUT_MAX_OUTPUT_TOKENS", path)),
        worker_max_output_tokens=int(_positive_int(fields, "WORKER_MAX_OUTPUT_TOKENS", path)),
        final_max_output_tokens=int(_positive_int(fields, "FINAL_MAX_OUTPUT_TOKENS", path)),
    )
