from __future__ import annotations

import re

# Hashtags are entered inline in the description, the way YouTube, Facebook
# and Instagram all read them -- there's no separate tags field. This module
# pulls them back out for the platforms that need them elsewhere: YouTube's
# hidden snippet.tags, and TikTok, whose caption is the title only.
#
# A hashtag is `#` + word characters with at least one non-digit (`#1` isn't
# one on any platform). The lookbehind skips `#` inside words, URLs
# (`page#section`) and HTML entities (`&#39;`).
_HASHTAG_RE = re.compile(r"(?<![\w#&/])#(\w*[^\W\d]\w*)")


def extract(text: str) -> list[str]:
    """Hashtags in `text`, without the `#`, in order of first appearance.
    Platforms treat hashtags case-insensitively, so later repeats that
    differ only in case are dropped."""
    seen: set[str] = set()
    tags: list[str] = []
    for tag in _HASHTAG_RE.findall(text):
        if tag.lower() not in seen:
            seen.add(tag.lower())
            tags.append(tag)
    return tags


def from_phrase(phrase: str) -> str:
    """`metal guitar` -> `#metalguitar`. YouTube's hidden tags (what the Tag
    inspector shows) can contain spaces and punctuation; hashtags can't.
    Returns "" when nothing usable is left."""
    body = re.sub(r"\W", "", phrase)
    return f"#{body}" if _HASHTAG_RE.fullmatch(f"#{body}") else ""


def append(text: str, tags: list[str], separator: str = " ", max_len: int | None = None) -> str:
    """Appends `#tag` for each of `tags` not already in `text`. With
    `max_len`, tags that would push the result over it are skipped rather
    than truncated mid-word."""
    present = {t.lower() for t in extract(text)}
    result = text.rstrip()
    for tag in tags:
        if tag.lower() in present:
            continue
        candidate = f"{result}{separator if result else ''}#{tag}"
        if max_len is not None and len(candidate) > max_len:
            continue
        result = candidate
        present.add(tag.lower())
    return result
