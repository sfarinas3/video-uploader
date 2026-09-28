import pytest

from video_uploader.core.types import JobHandle, PlatformJobStatus, PlatformMetadata, VideoFile
from video_uploader.publishers import instagram
from video_uploader.publishers.instagram import InstagramProcessingError, InstagramPublisher


class _FakeResponse:
    def __init__(self, json_data=None, status_code=200):
        self._json_data = json_data if json_data is not None else {}
        self.status_code = status_code

    def raise_for_status(self):
        pass

    def json(self):
        return self._json_data


class FakeHttpClient:
    """Stands in for httpx.Client -- no network. Routes calls by URL shape
    and by the `fields` param, since a single upload() drives several
    different GET/POST calls in sequence."""

    def __init__(
        self,
        container_response=None,
        rupload_response=None,
        publish_response=None,
        publish_exception=None,
        status_code_queue=None,
        recover_response=None,
        existence_response=None,
        permalink_response=None,
    ):
        self._container_response = container_response or _FakeResponse({"id": "container123"})
        self._rupload_response = rupload_response or _FakeResponse({"success": True})
        self._publish_response = publish_response
        self._publish_exception = publish_exception
        self._status_code_queue = list(status_code_queue or ["FINISHED"])
        self._recover_response = recover_response
        self._existence_response = existence_response
        self._permalink_response = permalink_response
        # The initial "wait for container to finish processing" poll and
        # the post-publish-timeout recovery check both GET status_code --
        # this flag is what tells the fake apart, same as the real API
        # call sequence would (recovery only happens after media_publish
        # was actually attempted).
        self._publish_attempted = False

    def post(self, url, data=None, headers=None, content=None):
        if "rupload.facebook.com" in url:
            return self._rupload_response
        if url.endswith("/media_publish"):
            self._publish_attempted = True
            if self._publish_exception is not None:
                raise self._publish_exception
            return self._publish_response or _FakeResponse({"id": "published123"})
        return self._container_response

    def get(self, url, params=None):
        fields = (params or {}).get("fields", "")
        if fields == "status_code":
            if self._publish_attempted and self._recover_response is not None:
                return self._recover_response
            status = self._status_code_queue.pop(0) if len(self._status_code_queue) > 1 else self._status_code_queue[0]
            return _FakeResponse({"status_code": status})
        if fields == "permalink":
            return self._permalink_response or _FakeResponse({"permalink": "https://instagram.com/p/abc"})
        return self._existence_response or _FakeResponse({"id": "published123"})


@pytest.fixture
def publisher():
    pub = InstagramPublisher()
    pub._ig_user_id = "ig123"
    pub._access_token = "fake-token"
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


def test_validate_rejects_missing_title_and_description(publisher, tmp_path):
    errors = publisher.validate(_video(tmp_path), PlatformMetadata(title="  ", description="  "))
    assert any("caption" in e for e in errors)


def test_validate_rejects_too_short_duration(publisher, tmp_path):
    video = _video(tmp_path)
    video.duration_seconds = 1  # under Instagram's 3s Reels minimum
    errors = publisher.validate(video, PlatformMetadata(title="Test"))
    assert any("below" in e and "minimum" in e for e in errors)


def test_validate_accepts_description_only(publisher, tmp_path):
    errors = publisher.validate(_video(tmp_path), PlatformMetadata(title="", description="A caption"))
    assert errors == []


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


def test_validate_rejects_invalid_format_variant(publisher, tmp_path):
    errors = publisher.validate(
        _video(tmp_path), PlatformMetadata(title="Test", format_variant="carousel")
    )
    assert any("format_variant" in e for e in errors)


@pytest.mark.parametrize(
    "title,description,expected",
    [
        ("Title", "", "Title"),
        ("", "Description", "Description"),
        ("Title", "Description", "Title\n\nDescription"),
    ],
)
def test_caption_mapping(publisher, title, description, expected):
    metadata = PlatformMetadata(title=title, description=description)
    assert publisher._caption(metadata) == expected


@pytest.mark.parametrize(
    "format_variant,expected_media_type",
    [("feed", "REELS"), ("reel", "REELS"), ("story", "STORIES"), (None, "REELS")],
)
def test_upload_maps_format_variant_to_media_type(publisher, tmp_path, format_variant, expected_media_type):
    captured = {}

    class CapturingHttpClient(FakeHttpClient):
        def post(self, url, data=None, headers=None, content=None):
            if data and "media_type" in data:
                captured["media_type"] = data["media_type"]
            return super().post(url, data=data, headers=headers, content=content)

    publisher._http = CapturingHttpClient()
    publisher.upload(_video(tmp_path), PlatformMetadata(title="Test", format_variant=format_variant))
    assert captured["media_type"] == expected_media_type


def test_upload_returns_job_handle_with_published_media_id(publisher, tmp_path):
    publisher._http = FakeHttpClient(publish_response=_FakeResponse({"id": "published999"}))
    handle = publisher.upload(_video(tmp_path), PlatformMetadata(title="Test"))
    assert handle.platform_native_id == "published999"


def test_upload_polls_until_finished(publisher, tmp_path, monkeypatch):
    monkeypatch.setattr(instagram.time, "sleep", lambda _: None)
    publisher._http = FakeHttpClient(status_code_queue=["IN_PROGRESS", "IN_PROGRESS", "FINISHED"])
    handle = publisher.upload(_video(tmp_path), PlatformMetadata(title="Test"))
    assert handle.platform_native_id == "published123"


def test_upload_raises_on_error_status_code(publisher, tmp_path, monkeypatch):
    monkeypatch.setattr(instagram.time, "sleep", lambda _: None)
    publisher._http = FakeHttpClient(status_code_queue=["ERROR"])
    with pytest.raises(InstagramProcessingError):
        publisher.upload(_video(tmp_path), PlatformMetadata(title="Test"))


def test_upload_times_out_if_never_finished(publisher, tmp_path, monkeypatch):
    monkeypatch.setattr(instagram, "UPLOAD_PROCESSING_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(instagram.time, "sleep", lambda _: None)
    publisher._http = FakeHttpClient(status_code_queue=["IN_PROGRESS"])
    with pytest.raises(InstagramProcessingError):
        publisher.upload(_video(tmp_path), PlatformMetadata(title="Test"))


def test_upload_recovers_media_id_after_publish_timeout_if_it_actually_published(
    publisher, tmp_path
):
    publisher._http = FakeHttpClient(
        publish_exception=TimeoutError("The read operation timed out"),
        recover_response=_FakeResponse({"status_code": "PUBLISHED"}),
    )
    handle = publisher.upload(_video(tmp_path), PlatformMetadata(title="Test"))
    assert handle.platform_native_id == "container123"


def test_upload_reraises_publish_timeout_when_not_actually_published(publisher, tmp_path):
    publisher._http = FakeHttpClient(
        publish_exception=TimeoutError("The read operation timed out"),
        recover_response=_FakeResponse({"status_code": "IN_PROGRESS"}),
    )
    with pytest.raises(TimeoutError):
        publisher.upload(_video(tmp_path), PlatformMetadata(title="Test"))


def test_get_status_published(publisher):
    publisher._http = FakeHttpClient(existence_response=_FakeResponse({"id": "published123"}))
    status = publisher.get_status(JobHandle(platform_job_id=1, platform_native_id="published123"))
    assert status.status == PlatformJobStatus.PUBLISHED


def test_get_status_handles_missing_media(publisher):
    publisher._http = FakeHttpClient(existence_response=_FakeResponse({}, status_code=404))
    status = publisher.get_status(JobHandle(platform_job_id=1, platform_native_id="missing"))
    assert status.status == PlatformJobStatus.FAILED


def test_get_permalink(publisher):
    publisher._http = FakeHttpClient(
        permalink_response=_FakeResponse({"permalink": "https://instagram.com/p/xyz"})
    )
    assert publisher.get_permalink("published123") == "https://instagram.com/p/xyz"
