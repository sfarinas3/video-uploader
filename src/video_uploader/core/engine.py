from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlmodel import Session, select

from video_uploader.core.types import (
    JobHandle,
    JobStatus,
    PlatformJobStatus,
    PlatformMetadata,
    VideoFile,
)
from video_uploader import video_inspect
from video_uploader.models import PlatformJob, UploadJob
from video_uploader.publishers import PLATFORM_PUBLISHERS


class CoreEngine:
    """Job orchestration: fan a video out into one PlatformJob per selected
    platform, run each independently, and never let one platform's failure
    affect another's (DESIGN.md §6.7)."""

    def __init__(self, session: Session):
        self.session = session

    def submit_job(
        self,
        video_path: Path,
        default_metadata: PlatformMetadata,
        platforms: list[str],
        platform_overrides: dict[str, PlatformMetadata] | None = None,
        default_format_variants: dict[str, str] | None = None,
        publish_at: datetime | None = None,
        tz: str | None = None,
    ) -> UploadJob:
        """`default_format_variants` is the configured fallback per platform
        (e.g. config.yaml's `platforms.youtube.default_format`), used when
        the caller doesn't explicitly set format_variant on an override.
        Precedence: explicit override > configured default > (M8) publisher
        auto-detection from the video, applied later at run time if this
        ends up None."""
        platform_overrides = platform_overrides or {}
        default_format_variants = default_format_variants or {}

        upload_job = UploadJob(
            video_path=str(video_path),
            default_title=default_metadata.title,
            default_description=default_metadata.description,
            default_tags=default_metadata.tags,
            default_privacy=default_metadata.privacy,
            scheduled_for=publish_at,
            scheduled_tz=tz,
        )
        self.session.add(upload_job)
        self.session.flush()  # assign upload_job.id without committing yet

        for platform in platforms:
            override = platform_overrides.get(platform)
            format_variant = (override.format_variant if override else None) or (
                default_format_variants.get(platform)
            )
            self.session.add(
                PlatformJob(
                    upload_job_id=upload_job.id,
                    platform=platform,
                    title_override=override.title if override else None,
                    description_override=override.description if override else None,
                    tags_override=override.tags if override else None,
                    privacy_override=override.privacy if override else None,
                    thumbnail_path=str(override.thumbnail_path)
                    if override and override.thumbnail_path
                    else None,
                    format_variant=format_variant,
                    status=PlatformJobStatus.PENDING,
                )
            )

        self.session.commit()
        self.session.refresh(upload_job)
        return upload_job

    def run_job(self, upload_job_id: int) -> None:
        """Run every pending platform job for this upload. Each platform is
        isolated: an exception or missing publisher fails only that
        platform's row."""
        upload_job = self.session.get(UploadJob, upload_job_id)
        if upload_job is None:
            raise ValueError(f"No UploadJob with id {upload_job_id}")

        video = self._build_video_file(Path(upload_job.video_path))

        for platform_job in upload_job.platform_jobs:
            if platform_job.status != PlatformJobStatus.PENDING:
                continue
            self._run_platform_job(platform_job, upload_job, video)

    def retry_platform_job(self, platform_job_id: int) -> None:
        """Re-run a single platform job without touching its siblings
        (DESIGN.md §6.11)."""
        platform_job = self.session.get(PlatformJob, platform_job_id)
        if platform_job is None:
            raise ValueError(f"No PlatformJob with id {platform_job_id}")
        upload_job = platform_job.upload_job
        video = self._build_video_file(Path(upload_job.video_path))
        platform_job.retry_count += 1
        self._run_platform_job(platform_job, upload_job, video)

    def _build_video_file(self, video_path: Path) -> VideoFile:
        if not video_path.exists():
            return VideoFile(path=video_path, size_bytes=0)
        inspected = video_inspect.inspect_video(video_path)
        return VideoFile(
            path=video_path, size_bytes=video_path.stat().st_size, **inspected
        )

    def refresh_platform_job_status(self, platform_job_id: int) -> None:
        """Re-check a platform job's current status without re-uploading --
        unlike retry_platform_job, this never calls upload() again, so it's
        safe to use while a platform is still processing an upload
        asynchronously (e.g. YouTube's uploaded -> processed transition)."""
        platform_job = self.session.get(PlatformJob, platform_job_id)
        if platform_job is None:
            raise ValueError(f"No PlatformJob with id {platform_job_id}")
        if not platform_job.platform_video_id:
            self._mark_failed(platform_job, "No platform video id to refresh status for")
            return

        publisher_cls = PLATFORM_PUBLISHERS.get(platform_job.platform)
        if publisher_cls is None:
            self._mark_failed(
                platform_job, f"No publisher registered for platform '{platform_job.platform}'"
            )
            return

        try:
            publisher = publisher_cls()
            publisher.authenticate()
            status = publisher.get_status(
                JobHandle(
                    platform_job_id=platform_job.id,
                    platform_native_id=platform_job.platform_video_id,
                )
            )
            platform_job.status = status.status
            platform_job.error_message = status.error_message
        except Exception as exc:  # noqa: BLE001 - one platform's failure must not raise
            self._mark_failed(platform_job, str(exc))
            return

        self.session.commit()

    def sweep_missed_jobs(self, as_of: datetime | None = None) -> list[int]:
        """Startup-only: mark PENDING platform jobs of already-past-due
        upload jobs MISSED. Must run once before the poller starts, so a
        job that was due while the app was closed is never silently fired
        late (DESIGN.md §4.4)."""
        as_of = as_of or datetime.now(timezone.utc)
        upload_jobs = self.session.exec(
            select(UploadJob).where(
                UploadJob.scheduled_for.is_not(None),
                UploadJob.scheduled_for < as_of,
            )
        ).all()

        affected_ids: list[int] = []
        for upload_job in upload_jobs:
            pending = [
                pj for pj in upload_job.platform_jobs if pj.status == PlatformJobStatus.PENDING
            ]
            if not pending:
                continue
            for platform_job in pending:
                platform_job.status = PlatformJobStatus.MISSED
            affected_ids.append(upload_job.id)

        self.session.commit()
        return affected_ids

    def list_due_upload_job_ids(self, as_of: datetime | None = None) -> list[int]:
        """For the poller: upload jobs whose scheduled time has arrived and
        still have at least one PENDING platform job."""
        as_of = as_of or datetime.now(timezone.utc)
        upload_jobs = self.session.exec(
            select(UploadJob).where(
                UploadJob.scheduled_for.is_not(None),
                UploadJob.scheduled_for <= as_of,
            )
        ).all()
        return [
            upload_job.id
            for upload_job in upload_jobs
            if any(pj.status == PlatformJobStatus.PENDING for pj in upload_job.platform_jobs)
        ]

    def reschedule_upload_job(self, upload_job_id: int, publish_at: datetime, tz: str) -> None:
        """Update an upload job's schedule and un-stick its MISSED platform
        jobs back to PENDING so the poller picks them up at the new time.
        FAILED jobs are untouched -- retrying those is a separate action
        (retry_platform_job)."""
        upload_job = self.session.get(UploadJob, upload_job_id)
        if upload_job is None:
            raise ValueError(f"No UploadJob with id {upload_job_id}")

        upload_job.scheduled_for = publish_at
        upload_job.scheduled_tz = tz
        for platform_job in upload_job.platform_jobs:
            if platform_job.status == PlatformJobStatus.MISSED:
                platform_job.status = PlatformJobStatus.PENDING

        self.session.commit()

    def _run_platform_job(
        self, platform_job: PlatformJob, upload_job: UploadJob, video: VideoFile
    ) -> None:
        metadata = PlatformMetadata(
            title=platform_job.title_override or upload_job.default_title,
            description=platform_job.description_override or upload_job.default_description,
            tags=platform_job.tags_override or upload_job.default_tags,
            privacy=platform_job.privacy_override or upload_job.default_privacy,
            thumbnail_path=Path(platform_job.thumbnail_path)
            if platform_job.thumbnail_path
            else None,
            format_variant=platform_job.format_variant,
        )

        publisher_cls = PLATFORM_PUBLISHERS.get(platform_job.platform)
        if publisher_cls is None:
            self._mark_failed(
                platform_job, f"No publisher registered for platform '{platform_job.platform}'"
            )
            return

        try:
            publisher = publisher_cls()
            publisher.authenticate()
            errors = publisher.validate(video, metadata)
            if errors:
                self._mark_failed(platform_job, "; ".join(errors))
                return

            platform_job.status = PlatformJobStatus.UPLOADING
            self.session.commit()

            handle = publisher.upload(video, metadata)
            platform_job.platform_video_id = handle.platform_native_id

            status: JobStatus = publisher.get_status(handle)
            platform_job.status = status.status
            platform_job.error_message = status.error_message
        except Exception as exc:  # noqa: BLE001 - one platform's failure must not raise
            self._mark_failed(platform_job, str(exc))
            return

        self.session.commit()

    def _mark_failed(self, platform_job: PlatformJob, message: str) -> None:
        platform_job.status = PlatformJobStatus.FAILED
        platform_job.error_message = message
        self.session.commit()
