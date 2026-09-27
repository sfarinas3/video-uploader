from pathlib import Path

from sqlmodel import select

from video_uploader.models import PlatformJob, UploadJob


def test_upload_job_and_platform_job_round_trip(session):
    job = UploadJob(video_path="video.mp4", default_title="Test video", default_tags=["a", "b"])
    session.add(job)
    session.flush()

    session.add(PlatformJob(upload_job_id=job.id, platform="youtube", status="pending"))
    session.add(PlatformJob(upload_job_id=job.id, platform="tiktok", status="pending"))
    session.commit()

    fetched = session.exec(select(UploadJob)).one()
    assert fetched.default_title == "Test video"
    assert fetched.default_tags == ["a", "b"]
    assert {pj.platform for pj in fetched.platform_jobs} == {"youtube", "tiktok"}


def test_platform_job_format_variant_defaults_to_none(session):
    job = UploadJob(video_path="video.mp4", default_title="Test")
    session.add(job)
    session.flush()

    pj = PlatformJob(upload_job_id=job.id, platform="instagram", status="pending")
    session.add(pj)
    session.commit()
    session.refresh(pj)

    assert pj.format_variant is None
