from __future__ import annotations

import logging
import re

# Query-string / form / JSON keys whose values are credentials. httpx logs
# every request URL at INFO, and the Facebook OAuth exchange and Graph API
# calls carry client_secret, the auth code and access tokens as query
# params -- without this they land in app.log in plain text.
SENSITIVE_KEYS = (
    "access_token",
    "client_secret",
    "fb_exchange_token",
    "refresh_token",
    "input_token",
    "appsecret_proof",
    "code",
)

REDACTED = "[REDACTED]"

_keys = "|".join(SENSITIVE_KEYS)
# key=value in URLs/forms. The lookbehind stops `code` matching inside
# e.g. `fields=status_code`.
_QUERY_RE = re.compile(rf"(?<![\w])({_keys})=([^&\s\"'#]+)", re.IGNORECASE)
# "key": "value" in JSON bodies echoed into error messages.
_JSON_RE = re.compile(rf"(\"(?:{_keys})\"\s*:\s*\")([^\"]*)(\")", re.IGNORECASE)


def redact(text: str) -> str:
    text = _QUERY_RE.sub(rf"\1={REDACTED}", text)
    return _JSON_RE.sub(rf"\1{REDACTED}\3", text)


class RedactSecretsFilter(logging.Filter):
    """Attach to handlers (not loggers) so it covers records propagated from
    every library, including httpx. Rewrites the record in place, so the
    redacted text is what every downstream handler sees."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = None
        if record.exc_info and not record.exc_text:
            # Formatter reuses a pre-set exc_text instead of re-formatting
            # exc_info, so redacting it here covers tracebacks too (an
            # httpx HTTPStatusError message includes the full URL).
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True
