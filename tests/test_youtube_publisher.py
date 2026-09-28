import pytest

from video_uploader.core.types import JobHandle, PlatformJobStatus, PlatformMetadata, VideoFile
from video_uploader.publishers.youtube import YouTubePublisher


class _Execute:
    def __init__(self, response):
        self._response = response

    def execute(self):
        return self._response


class _FakeChannelsResource:
    def __init__(self, response):
        self._response = response

    def list(self, part, mine):
        return _Execute(self._response)


class _FakePlaylistItemsResource:
    def __init__(self, response):
        self._response = response

    def list(self, part, playlistId, maxResults):
        return _Execute(self._response)


class _FakeThumbnailsResource:
    def __init__(self, exception=None, calls=None):
        self._exception = exception
        self._calls = calls if calls is not None else []

    def set(self, videoId, media_body):
        self._calls.append(videoId)
        if self._exception is not None:
            raise self._exception
        return _Execute({})


class FakeYouTubeClient:
    """Stands in for googleapiclient's youtube resource -- no network."""

    def __init__(
        self,
        insert_response=None,
        insert_exception=None,
        list_response=None,
        channels_response=None,
        playlist_items_response=None,
        thumbnail_exception=None,
    ):
        self._insert_response = insert_response
        self._insert_exception = insert_exception
        self._list_response = list_response
        self._channels_response = channels_response
        self._playlist_items_response = playlist_items_response
        self.thumbnail_calls: list[str] = []
        self._thumbnail_exception = thumbnail_exception

    def videos(self):
        return self

    def insert(self, part, body, media_body):
        if self._insert_exception is not None:
            raise self._insert_exception
        return _Execute(self._insert_response)

    def list(self, part, id):
        return _Execute(self._list_response)

    def channels(self):
        return _FakeChannelsResource(self._channels_response)

    def playlistItems(self):
        return _FakePlaylistItemsResource(self._playlist_items_response)

    def thumbnails(self):
        return _FakeThumbnailsResource(exception=self._thumbnail_exception, calls=self.thumbnail_calls)


@pytest.fixture
def publisher():
    pub = YouTubePublisher()
    pub._youtube = FakeYouTubeClient()  # authenticate() is never called in these tests
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


def test_validate_rejects_oversized_file(publisher, tmp_path):
    video = _video(tmp_path)
    video.size_bytes = 300 * 1024**3  # over YouTube's 256GB sanity ceiling
    errors = publisher.validate(video, PlatformMetadata(title="Test"))
    assert any("byte maximum" in e for e in errors)


def test_validate_rejects_title_too_long(publisher, tmp_path):
    errors = publisher.validate(_video(tmp_path), PlatformMetadata(title="x" * 101))
    assert any("100-character limit" in e for e in errors)


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


def test_upload_returns_job_handle_with_video_id(publisher, tmp_path):
    publisher._youtube = FakeYouTubeClient(insert_response={"id": "abc123"})
    handle = publisher.upload(_video(tmp_path), PlatformMetadata(title="Test", privacy="private"))
    assert handle.platform_native_id == "abc123"


def test_upload_sets_thumbnail_when_provided(publisher, tmp_path):
    thumb = tmp_path / "thumb.jpg"
    thumb.write_bytes(b"x")
    fake = FakeYouTubeClient(insert_response={"id": "abc123"})
    publisher._youtube = fake
    publisher.upload(
        _video(tmp_path),
        PlatformMetadata(title="Test", privacy="private", thumbnail_path=thumb),
    )
    assert fake.thumbnail_calls == ["abc123"]


def test_upload_succeeds_even_if_thumbnail_set_fails(publisher, tmp_path):
    thumb = tmp_path / "thumb.jpg"
    thumb.write_bytes(b"x")
    fake = FakeYouTubeClient(
        insert_response={"id": "abc123"}, thumbnail_exception=RuntimeError("boom")
    )
    publisher._youtube = fake
    handle = publisher.upload(
        _video(tmp_path),
        PlatformMetadata(title="Test", privacy="private", thumbnail_path=thumb),
    )
    assert handle.platform_native_id == "abc123"


@pytest.mark.parametrize(
    "upload_status,expected_status",
    [
        ("processed", PlatformJobStatus.PUBLISHED),
        ("uploaded", PlatformJobStatus.PROCESSING),
    ],
)
def test_get_status_maps_known_upload_statuses(publisher, upload_status, expected_status):
    publisher._youtube = FakeYouTubeClient(
        list_response={"items": [{"status": {"uploadStatus": upload_status}}]}
    )
    status = publisher.get_status(JobHandle(platform_job_id=1, platform_native_id="abc123"))
    assert status.status == expected_status


def test_get_status_maps_rejected_with_reason(publisher):
    publisher._youtube = FakeYouTubeClient(
        list_response={
            "items": [{"status": {"uploadStatus": "rejected", "rejectionReason": "copyright"}}]
        }
    )
    status = publisher.get_status(JobHandle(platform_job_id=1, platform_native_id="abc123"))
    assert status.status == PlatformJobStatus.FAILED
    assert status.error_message == "copyright"


def test_get_status_handles_missing_video(publisher):
    publisher._youtube = FakeYouTubeClient(list_response={"items": []})
    status = publisher.get_status(JobHandle(platform_job_id=1, platform_native_id="missing"))
    assert status.status == PlatformJobStatus.FAILED


def test_get_connected_channel_name_returns_title(publisher):
    publisher._youtube = FakeYouTubeClient(
        channels_response={"items": [{"snippet": {"title": "Hiraethum"}}]}
    )
    assert publisher.get_connected_channel_name() == "Hiraethum"


def test_get_connected_channel_name_returns_none_when_no_channel(publisher):
    publisher._youtube = FakeYouTubeClient(channels_response={"items": []})
    assert publisher.get_connected_channel_name() is None


def _iso_now_minus(seconds):
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def test_upload_recovers_video_id_after_timeout_if_it_actually_uploaded(publisher, tmp_path):
    publisher._youtube = FakeYouTubeClient(
        insert_exception=TimeoutError("The read operation timed out"),
        channels_response={"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UU1"}}}]},
        playlist_items_response={
            "items": [
                {
                    "snippet": {
                        "title": "Test video",
                        "publishedAt": _iso_now_minus(30),
                        "resourceId": {"videoId": "recovered123"},
                    }
                }
            ]
        },
    )
    handle = publisher.upload(
        _video(tmp_path), PlatformMetadata(title="Test video", privacy="private")
    )
    assert handle.platform_native_id == "recovered123"


def test_upload_reraises_timeout_when_nothing_was_actually_uploaded(publisher, tmp_path):
    publisher._youtube = FakeYouTubeClient(
        insert_exception=TimeoutError("The read operation timed out"),
        channels_response={"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UU1"}}}]},
        playlist_items_response={"items": []},
    )
    with pytest.raises(TimeoutError):
        publisher.upload(_video(tmp_path), PlatformMetadata(title="Test video", privacy="private"))


def test_upload_reraises_timeout_when_recent_upload_title_does_not_match(publisher, tmp_path):
    publisher._youtube = FakeYouTubeClient(
        insert_exception=TimeoutError("The read operation timed out"),
        channels_response={"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UU1"}}}]},
        playlist_items_response={
            "items": [
                {
                    "snippet": {
                        "title": "A different video",
                        "publishedAt": _iso_now_minus(30),
                        "resourceId": {"videoId": "unrelated123"},
                    }
                }
            ]
        },
    )
    with pytest.raises(TimeoutError):
        publisher.upload(_video(tmp_path), PlatformMetadata(title="Test video", privacy="private"))
