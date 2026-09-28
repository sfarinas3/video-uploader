from pathlib import Path

import pytest

from video_uploader.core.types import VideoFile
from video_uploader.preflight import check_preflight


def _video(**overrides) -> VideoFile:
    defaults = dict(
        path=Path("video.mp4"),
        size_bytes=1000,
        duration_seconds=None,
        codec=None,
        width=None,
        height=None,
    )
    defaults.update(overrides)
    return VideoFile(**defaults)


@pytest.mark.parametrize("platform", ["youtube", "facebook", "instagram", "tiktok"])
def test_all_none_inspection_fields_never_produce_errors(platform):
    video = _video()
    assert check_preflight(video, platform) == []


@pytest.mark.parametrize(
    "platform,duration",
    [("facebook", 10), ("instagram", 10), ("tiktok", 10)],
)
def test_normal_duration_within_bounds_produces_no_errors(platform, duration):
    video = _video(duration_seconds=duration)
    assert check_preflight(video, platform) == []


@pytest.mark.parametrize("platform,min_duration", [("instagram", 3), ("tiktok", 3), ("facebook", 1)])
def test_duration_below_minimum_is_rejected(platform, min_duration):
    video = _video(duration_seconds=min_duration - 0.5)
    errors = check_preflight(video, platform)
    assert any("below" in e and "minimum" in e for e in errors)


@pytest.mark.parametrize(
    "platform,max_duration",
    [("instagram", 900), ("tiktok", 600), ("facebook", 241 * 60)],
)
def test_duration_above_maximum_is_rejected(platform, max_duration):
    video = _video(duration_seconds=max_duration + 1)
    errors = check_preflight(video, platform)
    assert any("exceeds" in e and "maximum" in e for e in errors)


def test_youtube_has_no_duration_check():
    video = _video(duration_seconds=999999)
    assert check_preflight(video, "youtube") == []


@pytest.mark.parametrize(
    "platform,max_bytes", [("youtube", 256 * 1024**3), ("facebook", 10 * 1024**3),
                            ("instagram", 1 * 1024**3), ("tiktok", 4 * 1024**3)]
)
def test_file_size_above_maximum_is_rejected(platform, max_bytes):
    video = _video(size_bytes=max_bytes + 1)
    errors = check_preflight(video, platform)
    assert any("exceeds" in e and "byte maximum" in e for e in errors)


@pytest.mark.parametrize("platform,codec", [("instagram", "h264"), ("tiktok", "h264")])
def test_allowed_codec_produces_no_error(platform, codec):
    video = _video(codec=codec)
    assert check_preflight(video, platform) == []


@pytest.mark.parametrize("platform", ["instagram", "tiktok"])
def test_disallowed_codec_is_rejected(platform):
    video = _video(codec="vp9")
    errors = check_preflight(video, platform)
    assert any("codec" in e for e in errors)


def test_youtube_and_facebook_have_no_codec_check():
    video = _video(codec="vp9")
    assert check_preflight(video, "youtube") == []
    assert check_preflight(video, "facebook") == []


def test_instagram_rejects_extreme_aspect_ratio():
    video = _video(width=2000, height=1)
    errors = check_preflight(video, "instagram")
    assert any("aspect ratio" in e for e in errors)


def test_instagram_accepts_normal_aspect_ratio():
    video = _video(width=1080, height=1920)
    assert check_preflight(video, "instagram") == []


def test_other_platforms_have_no_aspect_ratio_check():
    video = _video(width=2000, height=1)
    assert check_preflight(video, "youtube") == []
    assert check_preflight(video, "facebook") == []
    assert check_preflight(video, "tiktok") == []
