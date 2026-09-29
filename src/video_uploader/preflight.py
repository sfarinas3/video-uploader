from __future__ import annotations

from video_uploader.core.types import VideoFile

# Courtesy pre-flight checks, not a perfect mirror of each platform's own
# server-side validation -- the platform's own API response remains the
# final authority. A missing key means "not checked" for that platform;
# see docs/DESIGN.md milestone 8's design notes for why YouTube has no
# duration ceiling and why only Instagram gets an aspect-ratio bound.
PLATFORM_LIMITS: dict[str, dict] = {
    "youtube": {
        "max_size_bytes": 256 * 1024**3,
    },
    "facebook": {
        "min_duration_seconds": 1,
        "max_duration_seconds": 241 * 60,
        "max_size_bytes": 10 * 1024**3,
    },
    "instagram": {
        "min_duration_seconds": 3,
        # Not the documented "15 min Content Publishing API ceiling" -- that
        # figure doesn't hold for media_type=REELS, the only type this app
        # actually uses now that plain VIDEO is deprecated (see
        # instagram.py's FORMAT_TO_MEDIA_TYPE). Bisected live against the
        # real API with several minimal-bitrate probe clips to isolate
        # duration from file size: 75s uploads clean, 80s reliably 400s
        # with a bare rupload.facebook.com "ProcessingFailedError" -- no
        # size, bitrate, or codec involved, confirmed by holding file size
        # near-constant (~1.5MB) across both. 75s is the confirmed-safe
        # ceiling; the true cutoff is somewhere in (75, 80].
        "max_duration_seconds": 75,
        # Confirmed against Meta's own docs: Reels are capped at 300MB via
        # the rupload API. A file between this and the old 1GB courtesy
        # limit was passing this check and then getting a bare 400 Bad
        # Request from rupload.facebook.com with no useful message --
        # confirmed live.
        "max_size_bytes": 300 * 1024**2,
        "allowed_codecs": {"h264", "hevc"},
        "min_aspect_ratio": 0.01,
        "max_aspect_ratio": 10.0,
    },
    "tiktok": {
        "min_duration_seconds": 3,
        "max_duration_seconds": 600,  # 10 min
        "max_size_bytes": 4 * 1024**3,
        "allowed_codecs": {"h264"},
    },
}


def check_preflight(video: VideoFile, platform: str) -> list[str]:
    limits = PLATFORM_LIMITS.get(platform, {})
    errors: list[str] = []

    duration = video.duration_seconds
    if duration is not None:
        min_duration = limits.get("min_duration_seconds")
        if min_duration is not None and duration < min_duration:
            errors.append(
                f"Video duration ({duration:.1f}s) is below {platform}'s {min_duration}s minimum"
            )
        max_duration = limits.get("max_duration_seconds")
        if max_duration is not None and duration > max_duration:
            errors.append(
                f"Video duration ({duration:.1f}s) exceeds {platform}'s {max_duration}s maximum"
            )

    max_size = limits.get("max_size_bytes")
    if max_size is not None and video.size_bytes > max_size:
        errors.append(
            f"Video file size ({video.size_bytes} bytes) exceeds {platform}'s "
            f"{max_size} byte maximum"
        )

    allowed_codecs = limits.get("allowed_codecs")
    if allowed_codecs is not None and video.codec is not None:
        if video.codec.lower() not in allowed_codecs:
            errors.append(
                f"Video codec {video.codec!r} isn't supported by {platform} "
                f"(allowed: {sorted(allowed_codecs)})"
            )

    if video.width is not None and video.height is not None and video.height > 0:
        aspect_ratio = video.width / video.height
        min_ratio = limits.get("min_aspect_ratio")
        max_ratio = limits.get("max_aspect_ratio")
        if min_ratio is not None and aspect_ratio < min_ratio:
            errors.append(
                f"Video aspect ratio ({aspect_ratio:.4f}) is below {platform}'s "
                f"{min_ratio} minimum"
            )
        if max_ratio is not None and aspect_ratio > max_ratio:
            errors.append(
                f"Video aspect ratio ({aspect_ratio:.4f}) exceeds {platform}'s "
                f"{max_ratio} maximum"
            )

    return errors
