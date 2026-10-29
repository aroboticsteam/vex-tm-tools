"""Small image uploads for the public site (team logos, event map, sponsors).

Only raster formats are accepted (no SVG: a hostile SVG can carry script),
the bytes must really be that format (checked by magic number, not by the
client-supplied name/type), and files are capped so a lobby display never
downloads something huge.
"""
from __future__ import annotations

import os
import tempfile

from fastapi import HTTPException, UploadFile

from backend.modules.media import s3

MAX_IMAGE_BYTES = 4 * 1024 * 1024

_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
)


def sniff_extension(head: bytes) -> str | None:
    for magic, ext in _SIGNATURES:
        if head.startswith(magic):
            return ext
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


async def read_validated_image(file: UploadFile, max_bytes: int = MAX_IMAGE_BYTES) -> tuple[bytes, str]:
    """Reads the upload (bounded) and returns `(bytes, extension)`."""
    data = await file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(status_code=413, detail=f"Image is larger than {max_bytes // (1024 * 1024)} MB")
    ext = sniff_extension(data[:16])
    if ext is None:
        raise HTTPException(status_code=400, detail="Unsupported image; use PNG, JPEG, GIF or WebP")
    return data, ext


async def store_image(data: bytes, key: str) -> None:
    """Uploads `data` to `key` through the same multipart path as videos."""
    fd, path = tempfile.mkstemp(prefix="qecomp-img-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        await s3.upload_multipart(path, key)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
