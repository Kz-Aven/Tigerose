"""Small, provider-neutral helpers for IM media attachments."""

from __future__ import annotations

import mimetypes
import re
import uuid
from pathlib import Path
from typing import Any

from avent_paths import data_root

MAX_MEDIA_BYTES = 25 * 1024 * 1024
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]+")


def media_kind(mime: str, name: str = "") -> str:
    value = (mime or mimetypes.guess_type(name)[0] or "").lower()
    if value.startswith("image/"):
        return "image"
    if value.startswith("audio/"):
        return "audio"
    if value.startswith("video/"):
        return "video"
    return "file"


def attachment_from_path(path: str | Path, *, name: str = "", mime: str = "") -> dict[str, Any] | None:
    source = Path(path)
    if not source.is_file() or source.stat().st_size > MAX_MEDIA_BYTES:
        return None
    filename = name or source.name
    content_type = mime or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return {
        "path": str(source.resolve()),
        "name": filename,
        "mime": content_type,
        "kind": media_kind(content_type, filename),
        "size": source.stat().st_size,
    }


def save_media(data: bytes, *, platform: str, message_id: str, name: str = "", mime: str = "") -> dict[str, Any]:
    if not data:
        raise ValueError("empty IM media")
    if len(data) > MAX_MEDIA_BYTES:
        raise ValueError("IM media exceeds 25 MiB limit")
    filename = _SAFE_NAME.sub("_", Path(name or "attachment").name).strip(" .") or "attachment"
    target_dir = data_root() / "data" / "uploads" / "im" / platform / message_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{uuid.uuid4().hex[:8]}-{filename}"
    target.write_bytes(data)
    attachment = attachment_from_path(target, name=filename, mime=mime)
    if not attachment:
        raise ValueError("unable to persist IM media")
    return attachment


def text_summary(kind: str, name: str = "") -> str:
    labels = {"image": "图片", "audio": "语音", "video": "视频", "file": "文件"}
    label = labels.get(kind, "附件")
    return f"[{label}{': ' + name if name else ''}]"
