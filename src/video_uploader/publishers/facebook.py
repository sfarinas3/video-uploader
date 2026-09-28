from __future__ import annotations

import mimetypes
from datetime import datetime, timedelta, timezone

import httpx

from video_uploader import preflight, token_store
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

# Large-file transfer can legitimately take a while on a slow upload
# connection -- much longer than the 30s default used for every other,
# small/JSON Graph API call on self._http.
RESUMABLE_UPLOAD_TRANSFER_TIMEOUT_SECONDS = 1800.0

# Same 10-minute reconciliation window as the YouTube publisher -- a
# client-side timeout while waiting for the upload response doesn't mean
# the video wasn't actually created on Facebook's side (we hit this exact
# failure mode for real with YouTube; applying the fix proactively here).
TIMEOUT_RECOVERY_WINDOW_MINUTES = 10


class FacebookAuthError(RuntimeError):
    """Raised when no stored Facebook Page credentials exist yet."""


def complete_oauth(code: str, redirect_uri: str) -> dict:
    """Exchange an OAuth authorization `code` for a Page access token,
    save it under token_store's "facebook" key, and -- if the resolved
    Page has an Instagram Business account linked -- also save an
    "instagram" token entry reusing the same Page access token (Instagram
    Business publishing has no identity independent of its linked Page;
    see instagram.py). Returns the dict saved under "facebook".

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

    facebook_token = {
        "page_access_token": page["access_token"],
        "page_id": page["id"],
        "page_name": page["name"],
    }
    token_store.save_token("facebook", facebook_token)

    with httpx.Client(timeout=30.0) as client:
        ig_resp = client.get(
            f"{GRAPH_API_BASE}/{page['id']}",
            params={
                "fields": "instagram_business_account",
                "access_token": page["access_token"],
            },
        )
        ig_resp.raise_for_status()
        ig_account = ig_resp.json().get("instagram_business_account")

    if ig_account is not None:
        token_store.save_token(
            "instagram",
            {
                "page_access_token": page["access_token"],
                "ig_user_id": ig_account["id"],
                "page_id": page["id"],
            },
        )

    return facebook_token


class FacebookPublisher:
    def __init__(self):
        config = load_config()
        platform_config = config.platforms.get(PLATFORM, {})
        self._configured_page_id = platform_config.get("page_id", "")
        self._app_id = platform_config.get("app_id", "")
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

        errors.extend(preflight.check_preflight(video, PLATFORM))

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
        try:
            file_handle = self._upload_via_resumable_protocol(video)
            data = {
                "access_token": self._page_access_token,
                "title": metadata.title,
                "description": metadata.description,
                "fbuploader_video_file_chunk": file_handle,
                **self._privacy_params(metadata.privacy),
            }
            response = self._http.post(f"{GRAPH_API_BASE}/{self._page_id}/videos", data=data)
            response.raise_for_status()
            video_id = response.json()["id"]
        except (TimeoutError, ConnectionError, httpx.TimeoutException):
            recovered_id = self._find_recently_uploaded_video(metadata.title)
            if recovered_id is None:
                raise
            video_id = recovered_id

        if metadata.thumbnail_path:
            self._set_thumbnail_best_effort(video_id, metadata.thumbnail_path)

        return JobHandle(platform_job_id=-1, platform_native_id=video_id)

    def _upload_via_resumable_protocol(self, video: VideoFile) -> str:
        """Facebook's Graph API Resumable Upload protocol
        (developers.facebook.com/docs/graph-api/guides/upload) -- required
        for anything beyond trivially small files. The simple single
        multipart POST this replaced (straight to graph-video.facebook.com)
        hit a 413 well under Facebook's documented non-resumable size
        guidance, confirmed live with a 700MB test upload.

        Start: register an upload session and get back "upload:<id>".
        Transfer: stream the file's bytes to that session in one request
        (the protocol supports resuming a dropped transfer via a follow-up
        GET for the current offset, but a single request is sufficient and
        simplest for the file sizes this tool deals with). Returns the
        resulting file handle, later passed to the /{page_id}/videos
        publish call as fbuploader_video_file_chunk.
        """
        mime_type = mimetypes.guess_type(video.path.name)[0] or "video/mp4"
        start_resp = self._http.post(
            f"{GRAPH_API_BASE}/{self._app_id}/uploads",
            params={
                "file_name": video.path.name,
                "file_length": video.size_bytes,
                "file_type": mime_type,
                "access_token": self._page_access_token,
            },
        )
        start_resp.raise_for_status()
        upload_session_id = start_resp.json()["id"]  # "upload:<UPLOAD_SESSION_ID>"

        with video.path.open("rb") as fh:
            transfer_resp = self._http.post(
                f"{GRAPH_API_BASE}/{upload_session_id}",
                headers={
                    "Authorization": f"OAuth {self._page_access_token}",
                    "file_offset": "0",
                },
                content=fh,
                timeout=RESUMABLE_UPLOAD_TRANSFER_TIMEOUT_SECONDS,
            )
        transfer_resp.raise_for_status()
        return transfer_resp.json()["h"]

    def _set_thumbnail_best_effort(self, video_id: str, thumbnail_path) -> None:
        """Same reasoning as youtube.py's _set_thumbnail_best_effort: the
        video is already uploaded by this point, so a thumbnail failure
        must never fail the whole platform job and orphan a successful
        upload. Thumbnails are optional (DESIGN.md §6.4)."""
        try:
            with thumbnail_path.open("rb") as thumb_fh:
                self._http.post(
                    f"{GRAPH_API_BASE}/{video_id}/thumbnails",
                    data={"is_preferred": "true", "access_token": self._page_access_token},
                    files={"source": thumb_fh},
                )
        except Exception:  # noqa: BLE001 - best-effort, see docstring
            pass

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
