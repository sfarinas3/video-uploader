from __future__ import annotations

from typing import Protocol

from video_uploader.core.types import JobHandle, JobStatus, PlatformMetadata, VideoFile


class Publisher(Protocol):
    """Per-platform upload implementation. DESIGN.md §4.2."""

    def authenticate(self) -> None: ...

    def validate(self, video: VideoFile, metadata: PlatformMetadata) -> list[str]:
        """Return a list of human-readable validation errors, empty if OK."""
        ...

    def upload(self, video: VideoFile, metadata: PlatformMetadata) -> JobHandle: ...

    def get_status(self, job: JobHandle) -> JobStatus: ...
