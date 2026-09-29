from __future__ import annotations

import keyring
import keyring.errors
import pytest
from sqlmodel import Session, SQLModel, create_engine

from video_uploader.core.engine import CoreEngine
from video_uploader.core.types import JobHandle, JobStatus, PlatformJobStatus


@pytest.fixture(autouse=True)
def fake_keyring(monkeypatch):
    """Prevent every test from touching the real OS keychain."""
    store: dict[tuple[str, str], str] = {}

    def fake_set_password(service, username, password):
        store[(service, username)] = password

    def fake_get_password(service, username):
        return store.get((service, username))

    def fake_delete_password(service, username):
        if (service, username) not in store:
            raise keyring.errors.PasswordDeleteError("not found")
        del store[(service, username)]

    monkeypatch.setattr(keyring, "set_password", fake_set_password)
    monkeypatch.setattr(keyring, "get_password", fake_get_password)
    monkeypatch.setattr(keyring, "delete_password", fake_delete_password)
    return store


@pytest.fixture
def engine(tmp_path):
    # A real file, not sqlite:// in-memory: run_job now fans platform jobs
    # out across worker threads, each opening its own Session/connection.
    # An in-memory DB is a separate empty database per connection unless
    # forced onto one shared connection (StaticPool) -- but that made
    # genuinely concurrent threads share a single sqlite3 connection
    # object, which isn't safe and reproduced real, flaky lost writes.
    # A temp file matches production (also file-based) -- each thread gets
    # its own connection and SQLite's normal file locking serializes
    # writes correctly.
    db_path = tmp_path / "test.sqlite3"
    eng = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(eng)
    return eng


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture
def core_engine(session):
    return CoreEngine(session)


class FakePublisher:
    """Test-only Publisher: always succeeds, records what it was called with."""

    calls: list[str] = []

    def authenticate(self) -> None:
        FakePublisher.calls.append("authenticate")

    def validate(self, video, metadata) -> list[str]:
        FakePublisher.calls.append("validate")
        return []

    def upload(self, video, metadata) -> JobHandle:
        FakePublisher.calls.append("upload")
        return JobHandle(platform_job_id=-1, platform_native_id="fake-video-id")

    def get_status(self, job: JobHandle) -> JobStatus:
        FakePublisher.calls.append("get_status")
        return JobStatus(status=PlatformJobStatus.PUBLISHED)


class FlakyPublisher:
    """Test-only Publisher: authenticate()/get_status() fail with a
    ConnectionError a configurable number of times (simulating transient
    network errors) before succeeding, to exercise CoreEngine's
    retry.with_backoff wrapping around those two calls."""

    auth_failures_remaining = 0
    status_failures_remaining = 0
    calls: list[str] = []

    def authenticate(self) -> None:
        FlakyPublisher.calls.append("authenticate")
        if FlakyPublisher.auth_failures_remaining > 0:
            FlakyPublisher.auth_failures_remaining -= 1
            raise ConnectionError("transient")

    def validate(self, video, metadata) -> list[str]:
        return []

    def upload(self, video, metadata) -> JobHandle:
        return JobHandle(platform_job_id=-1, platform_native_id="flaky-video-id")

    def get_status(self, job: JobHandle) -> JobStatus:
        FlakyPublisher.calls.append("get_status")
        if FlakyPublisher.status_failures_remaining > 0:
            FlakyPublisher.status_failures_remaining -= 1
            raise ConnectionError("transient")
        return JobStatus(status=PlatformJobStatus.PUBLISHED)


class FailingPublisher:
    """Test-only Publisher: fails validate() every time."""

    def authenticate(self) -> None:
        pass

    def validate(self, video, metadata) -> list[str]:
        return ["video too long for this platform"]

    def upload(self, video, metadata) -> JobHandle:
        raise AssertionError("upload() should not be called when validate() fails")

    def get_status(self, job: JobHandle) -> JobStatus:
        raise AssertionError("get_status() should not be called when validate() fails")


@pytest.fixture
def registered_publishers(monkeypatch):
    """Registers FakePublisher for 'youtube' and FailingPublisher for
    'facebook'. All four real platforms (youtube/facebook/instagram/
    tiktok) are registered publishers as of milestone 6, so tests that
    need to exercise the 'no publisher registered' path use a made-up
    platform name like 'vimeo' instead -- CoreEngine doesn't validate
    platform names against the Platform enum, so any string works."""
    from video_uploader.publishers import PLATFORM_PUBLISHERS

    monkeypatch.setitem(PLATFORM_PUBLISHERS, "youtube", FakePublisher)
    monkeypatch.setitem(PLATFORM_PUBLISHERS, "facebook", FailingPublisher)
    FakePublisher.calls = []
    return PLATFORM_PUBLISHERS
