from datetime import datetime, timedelta, timezone

import pytest

from video_uploader.core.types import JobHandle, PlatformJobStatus, PlatformMetadata, VideoFile
from video_uploader.publishers.facebook import FacebookPublisher


class _FakeResponse:
    def __init__(self, json_data=None, status_code=200):
        self._json_data = json_data if json_data is not None else {}
        self.status_code = status_code

    def raise_for_status(self):
        pass

    def json(self):
        return self._json_data


class FakeHttpClient:
    """Stands in for httpx.Client -- no network. Upload now makes three
    POSTs (resumable-upload start, transfer, then the /videos publish
    call) instead of one -- post_response/post_exception apply to the
    final publish call, matching the pre-resumable-upload tests' intent
    ("the upload timed out but Facebook actually created the video")."""

    def __init__(
        self,
        post_response=None,
        post_exception=None,
        get_response=None,
        recovery_response=None,
        thumbnail_exception=None,
        start_response=None,
        transfer_response=None,
    ):
        self._post_response = post_response
        self._post_exception = post_exception
        self._get_response = get_response
        self._recovery_response = recovery_response
        self._get_call_count = 0
        self._thumbnail_exception = thumbnail_exception
        self.thumbnail_calls: list[str] = []
        self._start_response = start_response or _FakeResponse({"id": "upload:SESSION123"})
        self._transfer_response = transfer_response or _FakeResponse({"h": "HANDLE123"})
        self.calls: list[tuple[str, dict]] = []

    def post(self, url, data=None, files=None, params=None, headers=None, content=None, timeout=None):
        self.calls.append((url, {"data": data, "params": params, "headers": headers}))
        if url.endswith("/thumbnails"):
            self.thumbnail_calls.append(url)
            if self._thumbnail_exception is not None:
                raise self._thumbnail_exception
            return _FakeResponse({"success": True})
        if url.endswith("/uploads"):
            return self._start_response
        if "/upload:" in url:
            return self._transfer_response
        if self._post_exception is not None:
            raise self._post_exception
        return self._post_response

    def get(self, url, params=None):
        self._get_call_count += 1
        # First get() after a failed post() is the recovery lookup in
        # these tests; a bare status check uses _get_response directly.
        if self._recovery_response is not None:
            return self._recovery_response
        return self._get_response

    def delete(self, url, params=None):
        return _FakeResponse({"success": True})


@pytest.fixture
def publisher():
    pub = FacebookPublisher()
    pub._page_id = "123456"
    pub._app_id = "999"
    pub._page_access_token = "fake-page-token"
    pub._http = FakeHttpClient()  # authenticate() is never called in these tests
    return pub


def _video(tmp_path, size=4):
    path = tmp_path / "video.mp4"
    path.write_bytes(b"x" * size)
    return VideoFile(path=path, size_bytes=size)


def test_validate_rejects_missing_video_file(publisher, tmp_path):
    video = VideoFile(path=tmp_path / "missing.mp4", size_bytes=0)
    errors = publisher.validate(video, PlatformMetadata(title="Test"))
    assert any("not found" in e for e in errors)


def test_validate_rejects_too_short_duration(publisher, tmp_path):
    video = _video(tmp_path)
    video.duration_seconds = 0.5  # under Facebook's 1s minimum
    errors = publisher.validate(video, PlatformMetadata(title="Test"))
    assert any("below" in e and "minimum" in e for e in errors)


def test_validate_rejects_empty_title(publisher, tmp_path):
    errors = publisher.validate(_video(tmp_path), PlatformMetadata(title="   "))
    assert any("Title is required" in e for e in errors)


def test_validate_rejects_invalid_privacy(publisher, tmp_path):
    errors = publisher.validate(
        _video(tmp_path), PlatformMetadata(title="Test", privacy="super-public")
    )
    assert any("Invalid privacy" in e for e in errors)


def test_validate_passes_for_valid_input(publisher, tmp_path):
    errors = publisher.validate(
        _video(tmp_path), PlatformMetadata(title="Test", privacy="private")
    )
    assert errors == []


@pytest.mark.parametrize(
    "privacy,expected",
    [
        ("private", {"published": "false", "secret": "true"}),
        ("unlisted", {"published": "false"}),
        ("public", {"published": "true"}),
    ],
)
def test_privacy_maps_to_expected_params(publisher, privacy, expected):
    assert publisher._privacy_params(privacy) == expected


def test_upload_returns_job_handle_with_video_id(publisher, tmp_path):
    publisher._http = FakeHttpClient(post_response=_FakeResponse({"id": "abc123"}))
    handle = publisher.upload(_video(tmp_path), PlatformMetadata(title="Test", privacy="private"))
    assert handle.platform_native_id == "abc123"


def test_upload_goes_through_resumable_protocol_not_a_single_multipart_post(publisher, tmp_path):
    """Regression test: a single multipart POST straight to the /videos
    edge hit a 413 on real, unremarkable-sized videos (confirmed live) --
    upload() must go through the start/transfer/publish resumable-upload
    sequence instead, and use the transfer step's returned file handle."""
    fake = FakeHttpClient(post_response=_FakeResponse({"id": "abc123"}))
    publisher._http = fake

    publisher.upload(_video(tmp_path), PlatformMetadata(title="Test", privacy="private"))

    urls = [url for url, _ in fake.calls]
    assert urls == [
        "https://graph.facebook.com/v25.0/999/uploads",
        "https://graph.facebook.com/v25.0/upload:SESSION123",
        "https://graph.facebook.com/v25.0/123456/videos",
    ]
    publish_data = fake.calls[2][1]["data"]
    assert publish_data["fbuploader_video_file_chunk"] == "HANDLE123"


def test_upload_sets_thumbnail_when_provided(publisher, tmp_path):
    thumb = tmp_path / "thumb.jpg"
    thumb.write_bytes(b"x")
    fake = FakeHttpClient(post_response=_FakeResponse({"id": "abc123"}))
    publisher._http = fake
    publisher.upload(
        _video(tmp_path),
        PlatformMetadata(title="Test", privacy="private", thumbnail_path=thumb),
    )
    assert fake.thumbnail_calls == ["https://graph.facebook.com/v25.0/abc123/thumbnails"]


def test_upload_succeeds_even_if_thumbnail_set_fails(publisher, tmp_path):
    thumb = tmp_path / "thumb.jpg"
    thumb.write_bytes(b"x")
    fake = FakeHttpClient(
        post_response=_FakeResponse({"id": "abc123"}), thumbnail_exception=RuntimeError("boom")
    )
    publisher._http = fake
    handle = publisher.upload(
        _video(tmp_path),
        PlatformMetadata(title="Test", privacy="private", thumbnail_path=thumb),
    )
    assert handle.platform_native_id == "abc123"


@pytest.mark.parametrize(
    "video_status,expected_status",
    [
        ("ready", PlatformJobStatus.PUBLISHED),
        ("processing", PlatformJobStatus.PROCESSING),
        ("uploading", PlatformJobStatus.PROCESSING),
    ],
)
def test_get_status_maps_known_video_statuses(publisher, video_status, expected_status):
    publisher._http = FakeHttpClient(
        get_response=_FakeResponse({"status": {"video_status": video_status}})
    )
    status = publisher.get_status(JobHandle(platform_job_id=1, platform_native_id="abc123"))
    assert status.status == expected_status


def test_get_status_maps_error(publisher):
    publisher._http = FakeHttpClient(get_response=_FakeResponse({"status": {"video_status": "error"}}))
    status = publisher.get_status(JobHandle(platform_job_id=1, platform_native_id="abc123"))
    assert status.status == PlatformJobStatus.FAILED


def test_get_status_handles_missing_video(publisher):
    publisher._http = FakeHttpClient(get_response=_FakeResponse({}, status_code=404))
    status = publisher.get_status(JobHandle(platform_job_id=1, platform_native_id="missing"))
    assert status.status == PlatformJobStatus.FAILED


def test_upload_recovers_video_id_after_timeout_if_it_actually_uploaded(publisher, tmp_path):
    recent = (datetime.now(timezone.utc) - timedelta(seconds=30)).strftime(
        "%Y-%m-%dT%H:%M:%S+0000"
    )
    publisher._http = FakeHttpClient(
        post_exception=TimeoutError("The read operation timed out"),
        recovery_response=_FakeResponse(
            {"data": [{"id": "recovered123", "title": "Test video", "created_time": recent}]}
        ),
    )
    handle = publisher.upload(
        _video(tmp_path), PlatformMetadata(title="Test video", privacy="private")
    )
    assert handle.platform_native_id == "recovered123"


def test_upload_reraises_timeout_when_nothing_was_actually_uploaded(publisher, tmp_path):
    publisher._http = FakeHttpClient(
        post_exception=TimeoutError("The read operation timed out"),
        recovery_response=_FakeResponse({"data": []}),
    )
    with pytest.raises(TimeoutError):
        publisher.upload(_video(tmp_path), PlatformMetadata(title="Test video", privacy="private"))


def test_upload_reraises_timeout_when_recent_upload_title_does_not_match(publisher, tmp_path):
    recent = (datetime.now(timezone.utc) - timedelta(seconds=30)).strftime(
        "%Y-%m-%dT%H:%M:%S+0000"
    )
    publisher._http = FakeHttpClient(
        post_exception=TimeoutError("The read operation timed out"),
        recovery_response=_FakeResponse(
            {"data": [{"id": "unrelated123", "title": "A different video", "created_time": recent}]}
        ),
    )
    with pytest.raises(TimeoutError):
        publisher.upload(_video(tmp_path), PlatformMetadata(title="Test video", privacy="private"))
