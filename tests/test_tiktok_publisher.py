import pytest

from video_uploader.core.types import JobHandle, PlatformJobStatus, PlatformMetadata, VideoFile
from video_uploader.publishers.tiktok import TikTokProcessingError, TikTokPublisher


class _FakeResponse:
    def __init__(self, json_data=None, status_code=200):
        self._json_data = json_data if json_data is not None else {}
        self.status_code = status_code

    def raise_for_status(self):
        pass

    def json(self):
        return self._json_data


class FakeHttpClient:
    """Stands in for httpx.Client -- no network."""

    def __init__(
        self,
        privacy_level_options=None,
        init_response=None,
        put_exception=None,
        status_responses=None,
    ):
        self._privacy_level_options = (
            privacy_level_options if privacy_level_options is not None else ["SELF_ONLY"]
        )
        self._init_response = init_response or _FakeResponse(
            {"data": {"publish_id": "publish123", "upload_url": "https://upload.example/x"}}
        )
        self._put_exception = put_exception
        self._status_responses = list(status_responses or [{"status": "PUBLISH_COMPLETE"}])
        self.captured_init_body = None

    def post(self, url, headers=None, json=None):
        if url.endswith("/creator_info/query/"):
            return _FakeResponse({"data": {"privacy_level_options": self._privacy_level_options}})
        if url.endswith("/video/init/"):
            self.captured_init_body = json
            return self._init_response
        if url.endswith("/status/fetch/"):
            status = (
                self._status_responses.pop(0)
                if len(self._status_responses) > 1
                else self._status_responses[0]
            )
            return _FakeResponse({"data": status})
        raise AssertionError(f"unexpected POST to {url}")

    def put(self, url, headers=None, content=None):
        if self._put_exception is not None:
            raise self._put_exception
        return _FakeResponse({})


@pytest.fixture
def publisher():
    pub = TikTokPublisher()
    pub._access_token = "fake-access-token"
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


def test_validate_rejects_empty_title(publisher, tmp_path):
    errors = publisher.validate(_video(tmp_path), PlatformMetadata(title="   "))
    assert any("Title is required" in e for e in errors)


def test_validate_rejects_title_too_long(publisher, tmp_path):
    errors = publisher.validate(_video(tmp_path), PlatformMetadata(title="x" * 2201))
    assert any("exceeds TikTok's" in e for e in errors)


def test_validate_does_not_reject_any_privacy_value(publisher, tmp_path):
    errors = publisher.validate(
        _video(tmp_path), PlatformMetadata(title="Test", privacy="nonsense-value")
    )
    assert errors == []


def test_validate_rejects_thumbnail_path(publisher, tmp_path):
    thumb = tmp_path / "thumb.jpg"
    thumb.write_bytes(b"x")
    errors = publisher.validate(
        _video(tmp_path), PlatformMetadata(title="Test", thumbnail_path=thumb)
    )
    assert any("doesn't support custom thumbnail images" in e for e in errors)


@pytest.mark.parametrize("privacy", ["private", "unlisted", "public"])
def test_upload_always_sends_self_only_privacy_level(publisher, tmp_path, privacy):
    handle = publisher.upload(_video(tmp_path), PlatformMetadata(title="Test", privacy=privacy))
    assert publisher._http.captured_init_body["post_info"]["privacy_level"] == "SELF_ONLY"
    assert handle.platform_native_id == "publish123"


def test_upload_raises_if_self_only_not_offered(publisher, tmp_path):
    publisher._http = FakeHttpClient(privacy_level_options=["PUBLIC_TO_EVERYONE"])
    with pytest.raises(TikTokProcessingError):
        publisher.upload(_video(tmp_path), PlatformMetadata(title="Test"))


def test_upload_returns_job_handle_with_publish_id(publisher, tmp_path):
    publisher._http = FakeHttpClient(
        init_response=_FakeResponse(
            {"data": {"publish_id": "abc999", "upload_url": "https://upload.example/y"}}
        )
    )
    handle = publisher.upload(_video(tmp_path), PlatformMetadata(title="Test"))
    assert handle.platform_native_id == "abc999"


def test_upload_recovers_after_put_timeout_if_status_shows_something(publisher, tmp_path):
    publisher._http = FakeHttpClient(
        put_exception=TimeoutError("The read operation timed out"),
        status_responses=[{"status": "PROCESSING_UPLOAD"}],
    )
    handle = publisher.upload(_video(tmp_path), PlatformMetadata(title="Test"))
    assert handle.platform_native_id == "publish123"


def test_upload_reraises_put_timeout_when_nothing_received(publisher, tmp_path):
    publisher._http = FakeHttpClient(
        put_exception=TimeoutError("The read operation timed out"),
        status_responses=[{}],
    )
    with pytest.raises(TimeoutError):
        publisher.upload(_video(tmp_path), PlatformMetadata(title="Test"))


@pytest.mark.parametrize(
    "status,expected_status",
    [
        ("PUBLISH_COMPLETE", PlatformJobStatus.PUBLISHED),
        ("PROCESSING_UPLOAD", PlatformJobStatus.PROCESSING),
        ("PROCESSING_DOWNLOAD", PlatformJobStatus.PROCESSING),
        ("SEND_TO_USER_INBOX", PlatformJobStatus.PROCESSING),
    ],
)
def test_get_status_maps_known_statuses(publisher, status, expected_status):
    publisher._http = FakeHttpClient(status_responses=[{"status": status}])
    result = publisher.get_status(JobHandle(platform_job_id=1, platform_native_id="publish123"))
    assert result.status == expected_status


def test_get_status_maps_failure_with_reason(publisher):
    publisher._http = FakeHttpClient(
        status_responses=[{"status": "FAILED", "fail_reason": "video_pull_failed"}]
    )
    result = publisher.get_status(JobHandle(platform_job_id=1, platform_native_id="publish123"))
    assert result.status == PlatformJobStatus.FAILED
    assert result.error_message == "video_pull_failed"
