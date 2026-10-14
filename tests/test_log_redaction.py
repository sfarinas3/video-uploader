import logging

from video_uploader.log_redaction import RedactSecretsFilter, redact


def test_redacts_oauth_query_params():
    url = (
        "GET https://graph.facebook.com/v25.0/oauth/access_token?client_id=123"
        "&client_secret=s3cret&code=AQabc&fb_exchange_token=EAAx \"HTTP/1.1 200 OK\""
    )
    out = redact(url)
    assert "s3cret" not in out and "AQabc" not in out and "EAAx" not in out
    assert "client_id=123" in out
    assert "client_secret=[REDACTED]" in out
    assert out.endswith('"HTTP/1.1 200 OK"')


def test_does_not_touch_status_code_field():
    out = redact("GET https://graph.facebook.com/v25.0/1?fields=status_code&access_token=EAAy")
    assert "fields=status_code" in out
    assert "access_token=[REDACTED]" in out


def test_redacts_json_bodies():
    out = redact('{"access_token": "ya29.abc", "expires_in": 3600}')
    assert out == '{"access_token": "[REDACTED]", "expires_in": 3600}'


def test_filter_redacts_args_and_tracebacks():
    record = logging.LogRecord(
        "httpx", logging.INFO, __file__, 1, "HTTP Request: %s %s", ("GET", "https://x?access_token=EAAz"), None
    )
    try:
        raise RuntimeError("failed for url https://x?access_token=EAAz")
    except RuntimeError:
        import sys

        record.exc_info = sys.exc_info()

    assert RedactSecretsFilter().filter(record)
    formatted = logging.Formatter().format(record)
    assert "EAAz" not in formatted
    assert "access_token=[REDACTED]" in formatted.splitlines()[0]
