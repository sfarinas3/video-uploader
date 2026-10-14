from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class Platform(StrEnum):
    YOUTUBE = "youtube"
    FACEBOOK = "facebook"
    INSTAGRAM = "instagram"
    TIKTOK = "tiktok"


class PlatformJobStatus(StrEnum):
    PENDING = "pending"
    UPLOADING = "uploading"
    PROCESSING = "processing"
    PUBLISHED = "published"
    FAILED = "failed"
    MISSED = "missed"


@dataclass
class VideoFile:
    """A video on disk plus whatever inspection data is available.

    duration_seconds/codec/width/height are None until ffprobe-based
    preflight validation lands (DESIGN.md milestone 8) — publishers should
    treat them as optional.
    """

    path: Path
    size_bytes: int
    duration_seconds: float | None = None
    codec: str | None = None
    width: int | None = None
    height: int | None = None


@dataclass
class PlatformMetadata:
    title: str
    description: str = ""
    privacy: str = "private"
    thumbnail_path: Path | None = None
    # Video frame (ms from start) to use as the cover on platforms that pick
    # covers by timestamp rather than image upload (Instagram, TikTok).
    cover_time_ms: int | None = None
    # User override for platform-specific format (e.g. "shorts"/"standard"
    # for YouTube, "reel"/"feed"/"story" for Instagram). When None, the
    # publisher auto-detects the variant from the video's properties.
    format_variant: str | None = None


@dataclass
class JobHandle:
    platform_job_id: int
    platform_native_id: str | None = None


@dataclass
class JobStatus:
    status: PlatformJobStatus
    error_message: str | None = None
