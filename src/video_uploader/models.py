from datetime import datetime, timezone

from sqlmodel import Field, JSON, Relationship, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UploadJob(SQLModel, table=True):
    """One video submission, fanned out into one PlatformJob per target
    platform. Status is derived from its platform jobs, not stored
    independently, to avoid the two drifting out of sync."""

    id: int | None = Field(default=None, primary_key=True)
    video_path: str
    default_title: str
    default_description: str = ""
    default_tags: list[str] = Field(default_factory=list, sa_type=JSON)
    default_privacy: str = "private"
    created_at: datetime = Field(default_factory=utcnow)

    # None = publish now. Set = scheduled; the M3 poller fires it, or on
    # relaunch it's marked `missed` if the time passed while the app was
    # closed (DESIGN.md §4.4).
    scheduled_for: datetime | None = None
    scheduled_tz: str | None = None

    platform_jobs: list["PlatformJob"] = Relationship(back_populates="upload_job")


class PlatformJob(SQLModel, table=True):
    """One platform's upload within an UploadJob. Failure/retry is scoped
    to this row so one platform never blocks or rolls back the others
    (DESIGN.md §6.7, §6.11)."""

    id: int | None = Field(default=None, primary_key=True)
    upload_job_id: int = Field(foreign_key="uploadjob.id")
    platform: str  # video_uploader.core.types.Platform value

    title_override: str | None = None
    description_override: str | None = None
    tags_override: list[str] | None = Field(default=None, sa_type=JSON)
    privacy_override: str | None = None
    thumbnail_path: str | None = None
    # User override, e.g. "shorts"/"standard" (YouTube), "reel"/"feed"/
    # "story" (Instagram). None = publisher auto-detects from the video.
    format_variant: str | None = None

    status: str = "pending"  # PlatformJobStatus value
    platform_video_id: str | None = None
    error_message: str | None = None
    retry_count: int = 0

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    upload_job: UploadJob = Relationship(back_populates="platform_jobs")
