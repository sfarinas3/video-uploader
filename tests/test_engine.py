from datetime import datetime, timedelta, timezone
from pathlib import Path

from video_uploader.core.types import PlatformJobStatus, PlatformMetadata
from video_uploader.models import PlatformJob, UploadJob
from video_uploader.publishers import PLATFORM_PUBLISHERS

from conftest import FlakyPublisher


def _submit(core_engine, platforms, **kwargs):
    return core_engine.submit_job(
        video_path=Path("does-not-exist.mp4"),
        default_metadata=PlatformMetadata(title="Test video"),
        platforms=platforms,
        **kwargs,
    )


def test_fan_out_creates_one_platform_job_per_platform(core_engine, registered_publishers):
    job = _submit(core_engine, ["youtube", "facebook", "instagram"])
    assert {pj.platform for pj in job.platform_jobs} == {"youtube", "facebook", "instagram"}
    assert all(pj.status == PlatformJobStatus.PENDING for pj in job.platform_jobs)


def test_one_platform_failing_does_not_affect_others(core_engine, registered_publishers, session):
    # youtube -> FakePublisher (succeeds), facebook -> FailingPublisher
    # (validate fails), vimeo -> unregistered (made-up platform name).
    job = _submit(core_engine, ["youtube", "facebook", "vimeo"])
    core_engine.run_job(job.id)

    by_platform = {pj.platform: pj for pj in job.platform_jobs}
    assert by_platform["youtube"].status == PlatformJobStatus.PUBLISHED
    assert by_platform["youtube"].platform_video_id == "fake-video-id"

    assert by_platform["facebook"].status == PlatformJobStatus.FAILED
    assert "too long" in by_platform["facebook"].error_message

    assert by_platform["vimeo"].status == PlatformJobStatus.FAILED
    assert "No publisher registered" in by_platform["vimeo"].error_message


def test_retry_platform_job_only_touches_target(core_engine, registered_publishers, session):
    job = _submit(core_engine, ["youtube", "vimeo"])
    core_engine.run_job(job.id)

    by_platform = {pj.platform: pj for pj in job.platform_jobs}
    youtube_job = by_platform["youtube"]
    vimeo_job = by_platform["vimeo"]
    assert youtube_job.status == PlatformJobStatus.PUBLISHED
    assert vimeo_job.status == PlatformJobStatus.FAILED

    core_engine.retry_platform_job(vimeo_job.id)
    session.refresh(youtube_job)
    session.refresh(vimeo_job)

    # still fails (no publisher registered), but retry_count incremented,
    # and youtube's already-published job is untouched.
    assert vimeo_job.retry_count == 1
    assert vimeo_job.status == PlatformJobStatus.FAILED
    assert youtube_job.status == PlatformJobStatus.PUBLISHED


def test_format_variant_precedence(core_engine, session):
    job = core_engine.submit_job(
        video_path=Path("does-not-exist.mp4"),
        default_metadata=PlatformMetadata(title="Test"),
        platforms=["youtube", "instagram", "tiktok"],
        platform_overrides={"youtube": PlatformMetadata(title="Test", format_variant="shorts")},
        default_format_variants={"youtube": "standard", "instagram": "feed"},
    )
    by_platform = {pj.platform: pj for pj in job.platform_jobs}
    assert by_platform["youtube"].format_variant == "shorts"  # explicit override wins
    assert by_platform["instagram"].format_variant == "feed"  # falls back to configured default
    assert by_platform["tiktok"].format_variant is None  # no override, no default


def test_thumbnail_only_override_leaves_other_fields_at_job_defaults(core_engine, session):
    default_metadata = PlatformMetadata(
        title="Test", description="A description", tags=["a", "b"], privacy="unlisted"
    )
    job = core_engine.submit_job(
        video_path=Path("does-not-exist.mp4"),
        default_metadata=default_metadata,
        platforms=["youtube"],
        platform_overrides={
            "youtube": PlatformMetadata(
                title="Test",
                description="A description",
                tags=["a", "b"],
                privacy="unlisted",
                thumbnail_path=Path("thumb.jpg"),
            )
        },
    )
    youtube_job = job.platform_jobs[0]
    assert youtube_job.thumbnail_path == "thumb.jpg"
    assert youtube_job.title_override == job.default_title
    assert youtube_job.description_override == job.default_description
    assert youtube_job.tags_override == job.default_tags
    assert youtube_job.privacy_override == job.default_privacy


def test_privacy_defaults_to_private_and_can_be_overridden(core_engine, session):
    job = core_engine.submit_job(
        video_path=Path("does-not-exist.mp4"),
        default_metadata=PlatformMetadata(title="Test", privacy="unlisted"),
        platforms=["youtube", "instagram"],
        platform_overrides={
            "instagram": PlatformMetadata(title="Test", privacy="public"),
        },
    )
    by_platform = {pj.platform: pj for pj in job.platform_jobs}
    assert job.default_privacy == "unlisted"
    assert by_platform["youtube"].privacy_override is None  # falls back to job default
    assert by_platform["instagram"].privacy_override == "public"  # explicit override wins


def test_refresh_platform_job_status_does_not_reupload(core_engine, registered_publishers, session):
    FakePublisher = registered_publishers["youtube"]

    job = _submit(core_engine, ["youtube"])
    core_engine.run_job(job.id)
    youtube_job = job.platform_jobs[0]
    assert youtube_job.status == PlatformJobStatus.PUBLISHED
    assert FakePublisher.calls.count("upload") == 1

    core_engine.refresh_platform_job_status(youtube_job.id)
    session.refresh(youtube_job)

    assert FakePublisher.calls.count("upload") == 1  # unchanged -- no re-upload
    assert FakePublisher.calls.count("get_status") == 2  # once from run_job, once from refresh
    assert youtube_job.status == PlatformJobStatus.PUBLISHED


def test_refresh_platform_job_status_without_video_id_fails_clearly(core_engine, session):
    job = _submit(core_engine, ["youtube"])  # not run -- no platform_video_id yet
    youtube_job = job.platform_jobs[0]

    core_engine.refresh_platform_job_status(youtube_job.id)
    session.refresh(youtube_job)

    assert youtube_job.status == PlatformJobStatus.FAILED
    assert "No platform video id" in youtube_job.error_message


def test_sweep_missed_jobs_marks_only_past_due_pending_jobs(
    core_engine, registered_publishers, session
):
    now = datetime.now(timezone.utc)

    past_due = _submit(core_engine, ["youtube"], publish_at=now - timedelta(hours=1), tz="UTC")
    future = _submit(core_engine, ["youtube"], publish_at=now + timedelta(hours=1), tz="UTC")
    unscheduled = _submit(core_engine, ["youtube"])  # publish-now style, no schedule

    already_resolved = _submit(
        core_engine, ["youtube"], publish_at=now - timedelta(hours=2), tz="UTC"
    )
    core_engine.run_job(already_resolved.id)  # resolves to PUBLISHED before the sweep runs

    affected = core_engine.sweep_missed_jobs(as_of=now)

    session.refresh(past_due.platform_jobs[0])
    session.refresh(future.platform_jobs[0])
    session.refresh(unscheduled.platform_jobs[0])
    session.refresh(already_resolved.platform_jobs[0])

    assert affected == [past_due.id]
    assert past_due.platform_jobs[0].status == PlatformJobStatus.MISSED
    assert future.platform_jobs[0].status == PlatformJobStatus.PENDING
    assert unscheduled.platform_jobs[0].status == PlatformJobStatus.PENDING
    assert already_resolved.platform_jobs[0].status == PlatformJobStatus.PUBLISHED


def test_list_due_upload_job_ids_returns_only_due_and_pending(
    core_engine, registered_publishers, session
):
    now = datetime.now(timezone.utc)

    due = _submit(core_engine, ["youtube"], publish_at=now - timedelta(minutes=1), tz="UTC")
    future = _submit(core_engine, ["youtube"], publish_at=now + timedelta(hours=1), tz="UTC")

    resolved_but_due = _submit(
        core_engine, ["youtube"], publish_at=now - timedelta(minutes=1), tz="UTC"
    )
    core_engine.run_job(resolved_but_due.id)

    due_ids = core_engine.list_due_upload_job_ids(as_of=now)

    assert due_ids == [due.id]
    assert future.id not in due_ids
    assert resolved_but_due.id not in due_ids


def test_reschedule_upload_job_updates_schedule_and_resets_missed_only(
    core_engine, registered_publishers, session
):
    now = datetime.now(timezone.utc)
    job = _submit(core_engine, ["youtube", "facebook"], publish_at=now - timedelta(hours=1), tz="UTC")
    core_engine.sweep_missed_jobs(as_of=now)

    by_platform = {pj.platform: pj for pj in job.platform_jobs}
    session.refresh(by_platform["youtube"])
    assert by_platform["youtube"].status == PlatformJobStatus.MISSED

    # Manually fail the facebook row to prove reschedule leaves FAILED alone.
    by_platform["facebook"].status = PlatformJobStatus.FAILED
    session.add(by_platform["facebook"])
    session.commit()

    new_time = now + timedelta(days=1)
    core_engine.reschedule_upload_job(job.id, publish_at=new_time, tz="America/New_York")

    session.refresh(job)
    session.refresh(by_platform["youtube"])
    session.refresh(by_platform["facebook"])

    assert job.scheduled_tz == "America/New_York"
    assert by_platform["youtube"].status == PlatformJobStatus.PENDING
    assert by_platform["facebook"].status == PlatformJobStatus.FAILED


def test_transient_authenticate_failure_is_retried_and_succeeds(
    core_engine, monkeypatch, session
):
    monkeypatch.setattr("video_uploader.retry.time.sleep", lambda _: None)
    monkeypatch.setitem(PLATFORM_PUBLISHERS, "youtube", FlakyPublisher)
    FlakyPublisher.auth_failures_remaining = 2
    FlakyPublisher.status_failures_remaining = 0
    FlakyPublisher.calls = []

    job = _submit(core_engine, ["youtube"])
    core_engine.run_job(job.id)

    youtube_job = job.platform_jobs[0]
    assert youtube_job.status == PlatformJobStatus.PUBLISHED
    assert FlakyPublisher.calls.count("authenticate") == 3  # 2 failures + 1 success


def test_persistent_get_status_failure_still_fails_the_job(core_engine, monkeypatch, session):
    monkeypatch.setattr("video_uploader.retry.time.sleep", lambda _: None)
    monkeypatch.setitem(PLATFORM_PUBLISHERS, "youtube", FlakyPublisher)
    FlakyPublisher.auth_failures_remaining = 0
    FlakyPublisher.status_failures_remaining = 99  # never recovers within max_attempts
    FlakyPublisher.calls = []

    job = _submit(core_engine, ["youtube"])
    core_engine.run_job(job.id)

    youtube_job = job.platform_jobs[0]
    assert youtube_job.status == PlatformJobStatus.FAILED
    assert FlakyPublisher.calls.count("get_status") == 3  # retry.with_backoff's default max_attempts


def test_purge_old_job_history_removes_only_jobs_past_retention(
    core_engine, registered_publishers, session
):
    now = datetime.now(timezone.utc)
    old_job = _submit(core_engine, ["youtube"])
    old_job.created_at = now - timedelta(days=40)
    recent_job = _submit(core_engine, ["youtube"])
    recent_job.created_at = now - timedelta(days=5)
    session.commit()

    purged = core_engine.purge_old_job_history(retention_days=30, as_of=now)

    assert purged == [old_job.id]
    assert session.get(UploadJob, old_job.id) is None
    assert session.get(UploadJob, recent_job.id) is not None


def test_purge_old_job_history_cascade_deletes_platform_jobs(
    core_engine, registered_publishers, session
):
    now = datetime.now(timezone.utc)
    old_job = _submit(core_engine, ["youtube", "facebook"])
    old_job.created_at = now - timedelta(days=400)  # older than the 365-day max too
    platform_job_ids = [pj.id for pj in old_job.platform_jobs]
    session.commit()

    core_engine.purge_old_job_history(retention_days=365, as_of=now)

    for platform_job_id in platform_job_ids:
        assert session.get(PlatformJob, platform_job_id) is None


def test_purge_old_job_history_is_noop_when_nothing_is_old(
    core_engine, registered_publishers, session
):
    _submit(core_engine, ["youtube"])
    assert core_engine.purge_old_job_history(retention_days=30) == []
