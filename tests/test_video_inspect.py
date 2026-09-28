import subprocess

import pytest

from video_uploader import video_inspect


@pytest.fixture(scope="module")
def real_clip(tmp_path_factory):
    dest = tmp_path_factory.mktemp("video_inspect") / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=2:size=320x240:rate=10",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            "-pix_fmt",
            "yuv420p",
            str(dest),
        ],
        check=True,
        capture_output=True,
    )
    return dest


def test_inspect_video_returns_real_data(real_clip):
    result = video_inspect.inspect_video(real_clip)
    assert result["duration_seconds"] == pytest.approx(2.0, abs=0.5)
    assert result["codec"] == "h264"
    assert result["width"] == 320
    assert result["height"] == 240


def test_inspect_video_returns_none_fields_for_missing_file(tmp_path):
    result = video_inspect.inspect_video(tmp_path / "does-not-exist.mp4")
    assert result == {
        "duration_seconds": None,
        "codec": None,
        "width": None,
        "height": None,
    }


def test_inspect_video_returns_none_fields_for_garbage_file(tmp_path):
    garbage = tmp_path / "garbage.mp4"
    garbage.write_bytes(b"not a real video file")
    result = video_inspect.inspect_video(garbage)
    assert result["duration_seconds"] is None
    assert result["codec"] is None
