"""Local file upload — store under data/uploads, return absolute paths."""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile

from avent_paths import data_root, ensure_data_dirs

router = APIRouter(prefix="/api/uploads", tags=["uploads"])

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+")
_MAX_BYTES = 25 * 1024 * 1024  # 25MB


def _upload_root() -> Path:
    ensure_data_dirs()
    return data_root() / "data" / "uploads"

def _safe_filename(name: str) -> str:
    base = Path(name or "file").name
    cleaned = _SAFE_NAME.sub("_", base).strip("._") or "file"
    return cleaned[:180]


@router.post("")
async def upload_file(file: UploadFile = File(...)):
    raw_name = file.filename or "file"
    data = await file.read()
    if not data:
        raise HTTPException(400, "empty file")
    if len(data) > _MAX_BYTES:
        raise HTTPException(400, f"file too large (max {_MAX_BYTES // (1024 * 1024)}MB)")

    batch = uuid.uuid4().hex[:12]
    dest_dir = _upload_root() / batch
    dest_dir.mkdir(parents=True, exist_ok=True)
    filename = _safe_filename(raw_name)
    dest = dest_dir / filename
    # avoid overwrite collisions
    if dest.exists():
        dest = dest_dir / f"{uuid.uuid4().hex[:6]}_{filename}"
    dest.write_bytes(data)

    mime = file.content_type or "application/octet-stream"
    abs_path = str(dest.resolve())
    return {
        "path": abs_path,
        "name": dest.name,
        "mime": mime,
        "size": len(data),
        "is_image": mime.startswith("image/"),
    }
