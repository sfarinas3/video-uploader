from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx

from video_uploader import token_store
from video_uploader.config import load_config
from video_uploader.core.types import (
    JobHandle,
    JobStatus,
    PlatformJobStatus,
    PlatformMetadata,
    VideoFile,
)

PLATFORM = "facebook"
GRAPH_API_VERSION = "v25.0"
GRAPH_API_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"
GRAPH_VIDEO_BASE = f"https://graph-video.facebook.com/{GRAPH_API_VERSION}"

# Same 10-minute reconciliation window as the YouTube publisher -- a
# client-side timeout while waiting for the upload response doesn't mean
# the video wasn't actually created on Facebook's side (we hit this exact
# failure mode for real with YouTube; applying the fix proactively here).
TIMEOUT_RECOVERY_WINDOW_MINUTES = 10


class FacebookAuthError(RuntimeError):
    """Raised when no stored Facebook Page credentials exist yet."""


def complete_oauth(code: str, redirect_uri: str) -> dict:
    """Exchange an OAuth authorization `code` for a Page access token and
    return the dict to pass to token_store.save_token("facebook", ...).

    Plain function (not a method) so it can be called from both the main
    app and the separate HTTPS OAuth catcher
    (video_uploader.oauth_https_catcher) -- Facebook requires HTTPS for
    the redirect URI with no way to disable that for this app, so the
    callback is handled by a small dedicated HTTPS listener rather than
    the main (plain HTTP, matching YouTube's already-working setup) app.
    """
    config = load_config()
    platform_config = config.platforms.get(PLATFORM, {})
    app_id = platform_config.get("app_id", "")
    app_secret = platform_config.get("app_secret", "")
    configured_page_id = platform_config.get("page_id", "")

    with httpx.Client(timeout=30.0) as client:
        token_resp = client.get(
            f"{GRAPH_API_BASE}/oauth/access_token",
            params={
                "client_id": app_id,
                "redirect_uri": redirect_uri,
                "client_secret": app_secret,
                "code": code,
            },
        )
        token_resp.raise_for_status()
        short_lived_token = token_resp.json()["access_token"]

        # Exchange for a long-lived (~60 day) user token before looking up
        # page tokens, so the resulting page token stays valid as long as
        # possible.
        exchange_resp = client.get(
            f"{GRAPH_API_BASE}/oauth/access_token",
            params={
                "grant_type": "fb_exchange_token",
                "client_id": app_id,
                "client_secret": app_secret,
                "fb_exchange_token": short_lived_token,
            },
        )
        exchange_resp.raise_for_status()
        long_lived_token = exchange_resp.json()["access_token"]

        accounts_resp = client.get(
            f"{GRAPH_API_BASE}/me/accounts", params={"access_token": long_lived_token}
        )
        accounts_resp.raise_for_status()
        pages = accounts_resp.json().get("data", [])

    page = next((p for p in pages if p["id"] == configured_page_id), None)
    if page is None:
        available = ", ".join(f"{p['id']} ({p['name']})" for p in pages) or "none"
        raise ValueError(
            f"Configured Facebook page_id '{configured_page_id}' wasn't found among the "
            f"pages this account manages. Set platforms.facebook.page_id in config.yaml "
            f"to one of: {available}"
        )

    return {
        "page_access_token": page["access_token"],
        "page_id": page["id"],
        "page_name": page["name"],
    }


class FacebookPublisher:
    def __init__(self):
        config = load_config()
        platform_config = config.platforms.get(PLATFORM, {})
        self._configured_page_id = platform_config.get("page_id", "")
        self._page_id: str | None = None
        self._page_access_token: str | None = None
        self._http: httpx.Client | None = None

    def authenticate(self) -> None:
        token = token_store.load_token(PLATFORM)
        if not token or not token.get("page_access_token"):
            raise FacebookAuthError(
                "No stored Facebook credentials -- connect Facebook via /settings first"
            )
        self._page_id = token["page_id"]
        self._page_access_token = token["page_access_token"]
        self._http = httpx.Client(timeout=30.0)

    def validate(self, video: VideoFile, metadata: PlatformMetadata) -> list[str]:
        errors: list[str] = []

        if not video.path.exists() or video.size_bytes <= 0:
            errors.append(f"Video file not found or empty: {video.path}")

        if not metadata.title.strip():
            errors.append("Title is required")

        if metadata.thumbnail_path and not metadata.thumbnail_path.exists():
            errors.append(f"Thumbnail file not found: {metadata.thumbnail_path}")

        if metadata.privacy not in ("private", "unlisted", "public"):
            errors.append(f"Invalid privacy value: {metadata.privacy!r}")

        return errors

    def _privacy_params(self, privacy: str) -> dict:
        """Facebook Pages have no personal-profile-style privacy tiers --
        a Page has no "friends" audience, so every Page video is public-
        facing to some degree. "private" here maps to the most restricted
        option the Graph API actually offers (published=false, secret=true:
        hidden from the Page's timeline and from search, but still viewable
        by anyone with the direct permalink) -- not true access control.
        Confirmed live: the video object's own `privacy` field still reads
        "EVERYONE" even with these params set, which appears to be a
        vestigial field from this endpoint's personal-profile-upload days
        rather than a reflection of the secret/published restriction.
        """
        if privacy == "private":
            return {"published": "false", "secret": "true"}
        if privacy == "unlisted":
            return {"published": "false"}
        return {"published": "true"}

    def upload(self, video: VideoFile, metadata: PlatformMetadata) -> JobHandle:
        data = {
            "access_token": self._page_access_token,
            "title": metadata.title,
            "description": metadata.description,
            **self._privacy_params(metadata.privacy),
        }
        try:
            with video.path.open("rb") as fh:
                response = self._http.post(
                    f"{GRAPH_VIDEO_BASE}/{self._page_id}/videos",
                    data=data,
                    files={"source": fh},
                )
            response.raise_for_status()
        except (TimeoutError, ConnectionError, httpx.TimeoutException):
            recovered_id = self._find_recently_uploaded_video(metadata.title)
            if recovered_id is not None:
                return JobHandle(platform_job_id=-1, platform_native_id=recovered_id)
            raise

        return JobHandle(platform_job_id=-1, platform_native_id=response.json()["id"])

    def _find_recently_uploaded_video(self, title: str) -> str | None:
        response = self._http.get(
            f"{GRAPH_API_BASE}/{self._page_id}/videos",
            params={
                "fields": "title,created_time",
                "limit": 5,
                "access_token": self._page_access_token,
            },
        )
        response.raise_for_status()
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=TIMEOUT_RECOVERY_WINDOW_MINUTES)
        for item in response.json().get("data", []):
            if item.get("title") != title:
                continue
            created_at = datetime.fromisoformat(item["created_time"].replace("+0000", "+00:00"))
            if created_at >= cutoff:
                return item["id"]
        return None

    def get_status(self, job: JobHandle) -> JobStatus:
        response = self._http.get(
            f"{GRAPH_API_BASE}/{job.platform_native_id}",
            params={"fields": "status", "access_token": self._page_access_token},
        )
        if response.status_code == 404:
            return JobStatus(
                status=PlatformJobStatus.FAILED,
                error_message=f"Video {job.platform_native_id} not found on Facebook",
            )
        response.raise_for_status()

        video_status = response.json().get("status", {}).get("video_status")
        if video_status == "ready":
            return JobStatus(status=PlatformJobStatus.PUBLISHED)
        # "uploading" precedes "processing" -- confirmed live, not
        # documented alongside ready/processing/error on Meta's
        # video-status reference page.
        if video_status in ("uploading", "processing"):
            return JobStatus(status=PlatformJobStatus.PROCESSING)
        if video_status == "error":
            return JobStatus(status=PlatformJobStatus.FAILED, error_message="Facebook processing error")
        return JobStatus(
            status=PlatformJobStatus.PROCESSING,
            error_message=f"Unrecognized video_status: {video_status}",
        )

    def delete(self, platform_native_id: str) -> None:
        """Not part of the Publisher protocol. Used by the live smoke
        test's mandatory cleanup step (DESIGN.md §10)."""
        response = self._http.delete(
            f"{GRAPH_API_BASE}/{platform_native_id}",
            params={"access_token": self._page_access_token},
        )
        response.raise_for_status()
