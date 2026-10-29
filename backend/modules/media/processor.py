"""FFmpeg green-screen keying pipeline (plan §5.13 / Appendix A.5 / §C.2).

Owns: FFmpeg subprocess management, async polling of subprocess output,
progress reporting, temp file lifecycle.

May import from: `backend.core` (settings for temp dir), stdlib only.
Must NOT import from: `loader/`, `routers/`, `models/`, any other `modules/`
submodule.
"""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
from pathlib import Path

from backend.core.exceptions import ProcessingError
from backend.core.ntfy import send_ntfy_notification

logger = logging.getLogger(__name__)

# Per Appendix A.5, the pipeline always downscales to a maximum of 1920x1080
# before keying, regardless of source resolution.
_SCALE_PAD_FILTER = (
    "scale=1920:1080:force_original_aspect_ratio=decrease,"
    "pad=1920:1080:(ow-iw)/2:(oh-ih)/2"
)

_HEX_COLOUR_RE = re.compile(r"^#?[0-9A-Fa-f]{6}$")


def _hex_to_ffmpeg_colour(key_colour: str) -> str:
    """Convert a `#RRGGBB` hex string into FFmpeg's `colorkey` `0xRRGGBB` form.

    Raises `ProcessingError` if `key_colour` isn't a valid 6-digit hex colour.
    """
    if not _HEX_COLOUR_RE.match(key_colour or ""):
        raise ProcessingError(f"Invalid key colour: {key_colour!r} (expected '#RRGGBB')")
    return "0x" + key_colour.lstrip("#").upper()


def _clamp01(name: str, value: float) -> float:
    if not isinstance(value, (int, float)) or not (0.0 <= float(value) <= 1.0):
        raise ProcessingError(f"{name} must be a number in [0.0, 1.0], got {value!r}")
    return float(value)


#: Recorded team/bot clips (issue #1/#12) keep their own (usually square)
#: aspect ratio and frame rate: only downscaled so the height is at most 1080,
#: never padded to 16:9 (the padding would be opaque black bars on stream).
_CLIP_SCALE_FILTER = "scale=-2:'min(1080,ih)'"
CLIP_MAX_SECONDS = 12


def _build_ffmpeg_args(
    input_path: str,
    output_path: str,
    key_colour: str,
    similarity: float,
    blend: float,
    clip: bool = False,
) -> list[str]:
    ffmpeg_colour = _hex_to_ffmpeg_colour(key_colour)
    sim = _clamp01("similarity", similarity)
    bl = _clamp01("blend", blend)

    vf = (
        f"{_CLIP_SCALE_FILTER if clip else _SCALE_PAD_FILTER},"
        f"colorkey={ffmpeg_colour}:{sim}:{bl},"
        "format=yuva420p"
    )
    duration_args = ["-t", str(CLIP_MAX_SECONDS)] if clip else []

    # -y: overwrite output non-interactively (subprocess has no stdin to
    # answer FFmpeg's "overwrite? y/N" prompt).
    return [
        "ffmpeg",
        "-y",
        "-i",
        input_path,
        "-vf",
        vf,
        "-c:v",
        "libvpx-vp9",
        "-pix_fmt",
        "yuva420p",
        "-auto-alt-ref",
        "0",
        *duration_args,
        output_path,
    ]


async def _drain_stderr(stream: asyncio.StreamReader, tail: list[str], max_tail: int = 40) -> None:
    """Consume FFmpeg's stderr asynchronously (progress + diagnostics live
    here), keeping only the last `max_tail` lines for error reporting.

    FFmpeg writes a very large amount of progress output to stderr; failing
    to drain it would deadlock the subprocess once the OS pipe buffer fills.
    """
    while True:
        line = await stream.readline()
        if not line:
            break
        decoded = line.decode("utf-8", errors="replace").rstrip()
        if not decoded:
            continue
        logger.debug("ffmpeg: %s", decoded)
        tail.append(decoded)
        if len(tail) > max_tail:
            del tail[: len(tail) - max_tail]


async def process_video(
    input_path: str,
    output_path: str,
    key_colour: str,
    similarity: float,
    blend: float,
    clip: bool = False,
) -> None:
    """Run the FFmpeg green-screen keying pipeline, then re-raise on failure
    after firing an Appendix A.8 ntfy notification (see `_process_video` for
    the actual pipeline; kept separate so this notify-and-reraise wrapper
    stays a single, minimal choke point instead of touching each of
    `_process_video`'s several `ProcessingError` raise sites individually).
    """
    try:
        await _process_video(input_path, output_path, key_colour, similarity, blend, clip)
    except ProcessingError as exc:
        await send_ntfy_notification("Video processing failed", str(exc), priority="high")
        raise


async def _process_video(
    input_path: str,
    output_path: str,
    key_colour: str,
    similarity: float,
    blend: float,
    clip: bool = False,
) -> None:
    """Run the FFmpeg green-screen keying pipeline on `input_path`, writing a
    transparent VP9/webm to `output_path`.

    Per Appendix A.5: always downscales to a maximum of 1920x1080 first, then
    applies chroma keying with the given colour/similarity/blend, encoding to
    `libvpx-vp9` with an alpha channel (`yuva420p`).

    Raises `ProcessingError` on any failure (missing input, invalid
    parameters, ffmpeg not installed, non-zero exit code).
    """
    if shutil.which("ffmpeg") is None:
        raise ProcessingError("ffmpeg executable not found on PATH")

    in_path = Path(input_path)
    if not in_path.is_file():
        raise ProcessingError(f"Input video not found: {input_path}")

    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    args = _build_ffmpeg_args(str(in_path), str(out_path), key_colour, similarity, blend, clip)
    logger.info("Running ffmpeg: %s", " ".join(args))

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise ProcessingError(f"Failed to launch ffmpeg: {exc}") from exc

    stderr_tail: list[str] = []
    try:
        assert proc.stderr is not None
        await _drain_stderr(proc.stderr, stderr_tail)
        returncode = await proc.wait()
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        raise
    except Exception as exc:  # pragma: no cover - defensive
        proc.kill()
        await proc.wait()
        raise ProcessingError(f"ffmpeg execution failed: {exc}") from exc

    if returncode != 0:
        tail = "\n".join(stderr_tail)
        raise ProcessingError(
            f"ffmpeg exited with code {returncode} for input {input_path!r}:\n{tail}"
        )

    if not out_path.is_file() or out_path.stat().st_size == 0:
        raise ProcessingError(f"ffmpeg reported success but produced no output at {output_path!r}")
