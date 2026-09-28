import httpx
import pytest

from video_uploader.retry import with_backoff


def test_succeeds_immediately_without_retrying(monkeypatch):
    monkeypatch.setattr("video_uploader.retry.time.sleep", lambda _: None)
    calls = []

    def func():
        calls.append(1)
        return "ok"

    assert with_backoff(func) == "ok"
    assert len(calls) == 1


@pytest.mark.parametrize("exc", [ConnectionError("boom"), TimeoutError("boom"), httpx.ConnectError("boom")])
def test_retries_network_errors_then_succeeds(monkeypatch, exc):
    monkeypatch.setattr("video_uploader.retry.time.sleep", lambda _: None)
    calls = []

    def func():
        calls.append(1)
        if len(calls) < 3:
            raise exc
        return "ok"

    assert with_backoff(func, max_attempts=3) == "ok"
    assert len(calls) == 3


def test_reraises_after_max_attempts(monkeypatch):
    monkeypatch.setattr("video_uploader.retry.time.sleep", lambda _: None)
    calls = []

    def func():
        calls.append(1)
        raise ConnectionError("still failing")

    with pytest.raises(ConnectionError):
        with_backoff(func, max_attempts=3)
    assert len(calls) == 3


def test_does_not_retry_non_network_exception(monkeypatch):
    monkeypatch.setattr("video_uploader.retry.time.sleep", lambda _: None)
    calls = []

    def func():
        calls.append(1)
        raise ValueError("not a network error")

    with pytest.raises(ValueError):
        with_backoff(func, max_attempts=3)
    assert len(calls) == 1


def test_passes_through_args_and_kwargs(monkeypatch):
    monkeypatch.setattr("video_uploader.retry.time.sleep", lambda _: None)

    def func(a, b, c=None):
        return (a, b, c)

    assert with_backoff(func, 1, 2, c=3) == (1, 2, 3)
