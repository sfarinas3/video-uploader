from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

# Optional ffmpeg integration -- the only place the app shells out to it.
# Everything else (inspection, preflight) stays pure Python; this exists
# solely so a video too large for one platform (Instagram's resolution cap,
# see preflight.py) can still be posted there without the user exporting a
# second copy by hand. No ffmpeg on PATH means the preflight error stands.

# Visually lossless for a phone-sized Reel while keeping the file a
# fraction of the source's size; same settings were confirmed live to pass
# Instagram's rupload processing (2026-10-27).
_ENCODE_ARGS = [
    "-c:v", "libx264", "-profile:v", "high", "-preset", "medium", "-crf", "18",
    "-pix_fmt", "yuv420p",
    "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
    "-movflags", "+faststart",
]


class TranscodeError(RuntimeError):
    """ffmpeg exited non-zero. Carries the tail of its stderr."""


def find_ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def scaled_dimensions(width: int, height: int, max_short_side: int) -> tuple[int, int]:
    """Dimensions after shrinking the shorter side to max_short_side, keeping
    aspect ratio and rounding the other side to an even number (libx264's
    yuv420p needs both even). Matches the -2 in downscale()'s scale filter."""
    if min(width, height) <= max_short_side:
        return width, height
    if width <= height:
        return max_short_side, 2 * round(height * max_short_side / width / 2)
    return 2 * round(width * max_short_side / height / 2), max_short_side


def downscale(src: Path, dest: Path, max_short_side: int) -> Path:
    """Re-encodes src into dest with its shorter side at max_short_side.
    Raises TranscodeError if ffmpeg is missing or fails."""
    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        raise TranscodeError("ffmpeg not found on PATH")
    n = max_short_side
    # ffmpeg auto-rotates before filtering, so iw/ih here are the displayed
    # dimensions even for phone footage stored sideways with a rotation flag.
    scale = f"scale='if(lte(iw,ih),{n},-2)':'if(lte(iw,ih),-2,{n})':flags=lanczos"
    # The packaged app has no console; without this every run flashes a
    # cmd window on Windows.
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    result = subprocess.run(
        [ffmpeg, "-v", "error", "-y", "-i", str(src), "-vf", scale, *_ENCODE_ARGS, str(dest)],
        capture_output=True,
        text=True,
        creationflags=creationflags,
    )
    if result.returncode != 0:
        raise TranscodeError(f"ffmpeg failed (exit {result.returncode}): {result.stderr[-500:]}")
    return dest
