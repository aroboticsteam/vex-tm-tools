"""Real-FFmpeg tests for backend.modules.media.processor (plan §14 test_media.py).

These tests invoke the actual `ffmpeg`/`ffprobe` binaries on synthetically
generated (lavfi) test clips — no mocking of the subprocess itself, per the
task's verification requirement. Skipped if ffmpeg isn't on PATH.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from backend.core.exceptions import ProcessingError
from backend.modules.media.processor import _build_ffmpeg_args, _hex_to_ffmpeg_colour, process_video

pytestmark = pytest.mark.asyncio

FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _make_lavfi_clip(path: str, size: str = "3840x2160", colour: str = "0x00B140", duration: float = 0.2) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c={colour}:s={size}:d={duration}",
            "-vf",
            "format=yuv420p",
            path,
        ],
        check=True,
        capture_output=True,
    )


def test_hex_to_ffmpeg_colour():
    assert _hex_to_ffmpeg_colour("#00B140") == "0x00B140"
    assert _hex_to_ffmpeg_colour("00b140") == "0x00B140"


def test_hex_to_ffmpeg_colour_invalid():
    with pytest.raises(ProcessingError):
        _hex_to_ffmpeg_colour("not-a-colour")


def test_build_ffmpeg_args_includes_1080p_downscale_and_colorkey():
    args = _build_ffmpeg_args("in.mp4", "out.webm", "#00B140", 0.15, 0.05)
    vf = args[args.index("-vf") + 1]
    assert "scale=1920:1080:force_original_aspect_ratio=decrease" in vf
    assert "pad=1920:1080:(ow-iw)/2:(oh-ih)/2" in vf
    assert "colorkey=0x00B140:0.15:0.05" in vf
    assert "format=yuva420p" in vf
    assert args[0] == "ffmpeg"
    assert "-c:v" in args and "libvpx-vp9" in args
    assert "-pix_fmt" in args and "yuva420p" in args
    assert "-auto-alt-ref" in args and "0" in args


@pytest.mark.skipif(not FFMPEG_AVAILABLE, reason="ffmpeg/ffprobe not installed")
async def test_process_video_real_ffmpeg_produces_vp9_alpha_webm(tmp_path):
    input_path = str(tmp_path / "input.mp4")
    output_path = str(tmp_path / "output.webm")
    _make_lavfi_clip(input_path)

    await process_video(
        input_path=input_path,
        output_path=output_path,
        key_colour="#00B140",
        similarity=0.15,
        blend=0.05,
    )

    assert os.path.isfile(output_path)
    assert os.path.getsize(output_path) > 0

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", output_path],
        check=True,
        capture_output=True,
        text=True,
    )
    stream = json.loads(probe.stdout)["streams"][0]
    assert stream["codec_name"] == "vp9"
    # Always downscaled to a maximum of 1920x1080 (source here was 3840x2160).
    assert stream["width"] == 1920
    assert stream["height"] == 1080
    # WebM/VP9 alpha is carried as a BlockAdditional side channel — ffprobe
    # reports the opaque base layer's pix_fmt (yuv420p) even with alpha
    # present; the `alpha_mode` tag is the correct signal (verified manually
    # against an identical hand-run ffmpeg command during development).
    assert stream.get("tags", {}).get("alpha_mode") == "1"


@pytest.mark.skipif(not FFMPEG_AVAILABLE, reason="ffmpeg/ffprobe not installed")
async def test_process_video_small_source_not_upscaled(tmp_path):
    """A source smaller than 1920x1080 is padded to 1920x1080, not upscaled
    beyond it or left at its native size (per Appendix A.5's scale+pad filter)."""
    input_path = str(tmp_path / "small_input.mp4")
    output_path = str(tmp_path / "small_output.webm")
    _make_lavfi_clip(input_path, size="640x480")

    await process_video(input_path, output_path, "#00B140", 0.1, 0.05)

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", output_path],
        check=True,
        capture_output=True,
        text=True,
    )
    stream = json.loads(probe.stdout)["streams"][0]
    assert stream["width"] == 1920
    assert stream["height"] == 1080


async def test_process_video_missing_input_raises():
    with pytest.raises(ProcessingError, match="not found"):
        await process_video("/nonexistent/input.mp4", "/tmp/out.webm", "#00B140", 0.1, 0.05)


async def test_process_video_invalid_similarity_raises(tmp_path):
    input_path = str(tmp_path / "input.mp4")
    with open(input_path, "wb") as f:
        f.write(b"not a real video, but process_video should validate params first")

    with pytest.raises(ProcessingError, match="similarity"):
        await process_video(input_path, str(tmp_path / "out.webm"), "#00B140", 1.5, 0.05)


def _probe_stream(path: str) -> dict:
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", path],
        check=True,
        capture_output=True,
        text=True,
    )
    data = json.loads(probe.stdout)
    return {**data["streams"][0], "_format": data["format"]}


def test_build_ffmpeg_args_clip_mode_does_not_pad_or_force_16_9():
    args = _build_ffmpeg_args("in.webm", "out.webm", "#00B140", 0.1, 0.05, clip=True)
    vf = args[args.index("-vf") + 1]
    assert "pad=" not in vf and "1920" not in vf
    assert "min(1080,ih)" in vf
    assert "colorkey=0x00B140:0.1:0.05" in vf
    assert args[args.index("-t") + 1] == "12"  # duration cap
    assert "-r" not in args  # frame rate is never forced down


@pytest.mark.skipif(not FFMPEG_AVAILABLE, reason="ffmpeg/ffprobe not installed")
async def test_process_clip_keeps_square_framing_and_60fps(tmp_path):
    input_path = str(tmp_path / "square.mp4")
    output_path = str(tmp_path / "clip.webm")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=0x00B140:s=1080x1080:r=60:d=0.5", "-pix_fmt", "yuv420p", input_path],
        check=True,
        capture_output=True,
    )
    await process_video(input_path, output_path, "#00B140", 0.1, 0.05, clip=True)
    stream = _probe_stream(output_path)
    assert (stream["width"], stream["height"]) == (1080, 1080)  # not padded to 16:9
    num, den = stream["r_frame_rate"].split("/")
    assert round(int(num) / int(den)) == 60
    assert stream.get("tags", {}).get("alpha_mode") == "1"


@pytest.mark.skipif(not FFMPEG_AVAILABLE, reason="ffmpeg/ffprobe not installed")
async def test_process_clip_downscales_tall_source_and_caps_duration(tmp_path):
    input_path = str(tmp_path / "big.mp4")
    output_path = str(tmp_path / "clip.webm")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=0x00B140:s=2160x2160:r=30:d=14", "-pix_fmt", "yuv420p", input_path],
        check=True,
        capture_output=True,
    )
    await process_video(input_path, output_path, "#00B140", 0.1, 0.05, clip=True)
    stream = _probe_stream(output_path)
    assert (stream["width"], stream["height"]) == (1080, 1080)
    assert float(stream["_format"]["duration"]) <= 12.5
