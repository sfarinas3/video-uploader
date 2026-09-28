from __future__ import annotations

import json
import subprocess
from pathlib import Path

# Graceful-degradation dependency: pre-flight validation is an
# optimization layer, not a hard requirement for the upload pipeline to
# function (DESIGN.md milestone 8's own design notes). If ffprobe isn't
# installed or a file can't be probed, every field below comes back None
# and downstream preflight checks simply skip -- VideoFile's fields are
# already documented as optional for exactly this reason
# (core/types.py).
_EMPTY_RESULT = {
    "duration_seconds": None,
    "codec": None,
    "width": None,
    "height": None,
}


def inspect_video(path: Path) -> dict:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(path),
            ],
            capture_output=True,
            timeout=30,
            check=True,
        )
        data = json.loads(result.stdout)
    except (
        FileNotFoundError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
    ):
        return dict(_EMPTY_RESULT)

    duration_seconds = None
    duration_raw = data.get("format", {}).get("duration")
    if duration_raw is not None:
        try:
            duration_seconds = float(duration_raw)
        except ValueError:
            pass

    video_stream = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "video"), None
    )
    if video_stream is None:
        return {"duration_seconds": duration_seconds, "codec": None, "width": None, "height": None}

    return {
        "duration_seconds": duration_seconds,
        "codec": video_stream.get("codec_name"),
        "width": video_stream.get("width"),
        "height": video_stream.get("height"),
    }
