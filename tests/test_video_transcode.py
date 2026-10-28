import shutil
import subprocess

import pytest

from video_uploader import video_inspect, video_transcode


@pytest.mark.parametrize(
    "width,height,expected",
    [
        (2160, 3840, (1080, 1920)),
        (3840, 2160, (1920, 1080)),
        (1440, 2560, (1080, 1920)),
        (2000, 2000, (1080, 1080)),
        # 1500x2666 -> 1919.52 tall; must land on an even number
        (1500, 2666, (1080, 1920)),
        # already within the cap -- untouched
        (1080, 1920, (1080, 1920)),
        (720, 1280, (720, 1280)),
    ],
)
def test_scaled_dimensions(width, height, expected):
    assert video_transcode.scaled_dimensions(width, height, 1080) == expected


def test_downscale_raises_without_ffmpeg(tmp_path, monkeypatch):
    monkeypatch.setattr(video_transcode, "find_ffmpeg", lambda: None)
    with pytest.raises(video_transcode.TranscodeError, match="not found"):
        video_transcode.downscale(tmp_path / "in.mp4", tmp_path / "out.mp4", 1080)


def test_downscale_reports_ffmpeg_failure(tmp_path):
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    bogus = tmp_path / "not_a_video.mp4"
    bogus.write_bytes(b"nope")
    with pytest.raises(video_transcode.TranscodeError, match="ffmpeg failed"):
        video_transcode.downscale(bogus, tmp_path / "out.mp4", 1080)


@pytest.mark.parametrize("size,expected", [("240x320", (120, 160)), ("320x240", (160, 120))])
def test_downscale_real_clip_matches_scaled_dimensions(tmp_path, size, expected):
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    src = tmp_path / "src.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"testsrc=duration=1:size={size}:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-c:v", "libx264", "-c:a", "aac", "-pix_fmt", "yuv420p", str(src),
        ],
        check=True,
    )
    out = video_transcode.downscale(src, tmp_path / "out.mp4", 120)

    info = video_inspect.inspect_video(out)
    assert (info["width"], info["height"]) == expected
    assert (info["width"], info["height"]) == video_transcode.scaled_dimensions(
        *map(int, size.split("x")), 120
    )
    # +faststart: moov precedes mdat
    head = out.read_bytes()
    assert head.find(b"moov") < head.find(b"mdat")
