from __future__ import annotations

import time
from typing import Callable, TypeVar

import httpx

T = TypeVar("T")

# Only network-level errors are worth retrying -- an auth failure or a
# 4xx response will fail identically on every attempt, so retrying those
# just wastes time before a failure that retrying can't fix. Not applied
# to upload() calls anywhere (see retry.py's callers) -- retrying those
# blindly risks creating a real duplicate post if the original request
# actually reached the platform before the error (DESIGN.md §7).
RETRYABLE_EXCEPTIONS = (ConnectionError, TimeoutError, httpx.TransportError)


def with_backoff(
    func: Callable[..., T],
    *args,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    **kwargs,
) -> T:
    for attempt in range(max_attempts):
        try:
            return func(*args, **kwargs)
        except RETRYABLE_EXCEPTIONS:
            if attempt == max_attempts - 1:
                raise
            time.sleep(base_delay * 2**attempt)
    raise AssertionError("unreachable")  # loop always returns or raises
