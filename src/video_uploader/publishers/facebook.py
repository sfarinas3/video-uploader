from __future__ import annotations

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

# Reels go through their own endpoint (/{page_id}/video_reels) -- a vertical
# clip sent to /videos lands as a regular video and never shows in the
# Page's Reels tab. format_variant "reel"/"video" forces one or the other;
# with neither, vertical videos up to Facebook's Reels length limit go up as
# Reels and everything else as a regular video.
REELS_MAX_DURATION_SECONDS = 90
FORMAT_VARIANTS = ("reel", "video")
REELS_RUPLOAD_BASE = f"https://rupload.facebook.com/video-upload/{GRAPH_API_VERSION}"
# Same offset/file_size chunking as instagram.py's rupload transfer
# (confirmed live for Reels too: 206 per partial chunk, 200 on the last).
REELS_UPLOAD_CHUNK_SIZE_BYTES = 5 * 1024 * 1024


class FacebookAuthError(RuntimeError):
    """Raised when no stored Facebook Page credentials exist yet."""


class FacebookUploadRejectedError(RuntimeError):
    """A Reels upload step was refused. Carries Facebook's own error body,
    which raise_for_status() would otherwise drop."""

    def __init__(self, step: str, status_code: int, body: str):
        super().__init__(f"Facebook rejected the Reel {step} (HTTP {status_code}): {body[:300]}")


def _is_reel(video: VideoFile, metadata: PlatformMetadata) -> bool:
    if metadata.format_variant:
        return metadata.format_variant == "reel"
    if not (video.width and video.height and video.duration_seconds):
        return False  # can't tell -- a regular video is the safe default
    return video.height > video.width and video.duration_seconds <= REELS_MAX_DURATION_SECONDS


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

        if metadata.format_variant and metadata.format_variant not in FORMAT_VARIANTS:
            errors.append(
                f"Invalid format_variant: {metadata.format_variant!r} "
                f"(expected one of {', '.join(FORMAT_VARIANTS)})"
            )
        elif (
            metadata.format_variant == "reel"
            and video.duration_seconds
            and video.duration_seconds > REELS_MAX_DURATION_SECONDS
        ):
            errors.append(
                f"Facebook Reels can be at most {REELS_MAX_DURATION_SECONDS}s "
                f"(this video is {round(video.duration_seconds)}s) -- use the 'video' format instead"
            )

        errors.extend(preflight.check_preflight(video, PLATFORM))

        return errors

    def _privacy_params(self, privacy: str) -> dict:
        """Facebook Pages have no personal-profile-style privacy tiers --
        a Page has no "friends" audience, so every Page video is public-
        facing to some degree. "private" here maps to the most restricted
        option the Graph API actually offers (published=false, secret=true:
        hidden from the Page's timeline and from search; confirmed live
        2026-10-14 that its permalink isn't viewable logged out either).
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
        upload_fn = self._upload_reel if _is_reel(video, metadata) else self._upload_via_chunked_protocol
        try:
            video_id = upload_fn(video, metadata)
        except (TimeoutError, ConnectionError, httpx.TimeoutException):
            recovered_id = self._find_recently_uploaded_video(metadata.title)
            if recovered_id is None:
                raise
            video_id = recovered_id

        if metadata.thumbnail_path:
            self._set_thumbnail_best_effort(video_id, metadata.thumbnail_path)

        return JobHandle(platform_job_id=-1, platform_native_id=video_id)

    def _upload_via_chunked_protocol(self, video: VideoFile, metadata: PlatformMetadata) -> str:
        """Facebook's chunked video upload protocol -- upload_phase=start/
        transfer/finish directly on /{page_id}/videos
        (developers.facebook.com/docs/graph-api/reference/page/videos/).

        Replaces an earlier attempt at the newer generic Resumable Upload
        API (POST /{app_id}/uploads + /upload:<id>): that one hit a 413 on
        the plain single-POST approach it replaced, then -- confirmed live
        with a real 700MB file -- returned a malformed multi-segment file
        handle from the transfer step instead of one clean handle, and
        neither passing that through as-is nor using just its last segment
        produced a video Facebook could actually process. This protocol
        sidesteps that: the *server* tells us the chunk size via each
        response's start_offset/end_offset, rather than us guessing a
        fixed size or sending the whole file in one shot.

        Start: register the session, get back video_id + the first
        start_offset/end_offset. Transfer: loop, sending exactly the byte
        range the last response asked for, until start_offset == end_offset
        (upload complete). Finish: attach title/description/privacy and
        publish. Returns the video_id from the start phase throughout.
        """
        start_resp = self._http.post(
            f"{GRAPH_API_BASE}/{self._page_id}/videos",
            data={
                "upload_phase": "start",
                "file_size": video.size_bytes,
                "access_token": self._page_access_token,
            },
        )
        start_resp.raise_for_status()
        start_data = start_resp.json()
        video_id = start_data["video_id"]
        upload_session_id = start_data["upload_session_id"]
        start_offset = int(start_data["start_offset"])
        end_offset = int(start_data["end_offset"])

        with video.path.open("rb") as fh:
            while start_offset != end_offset:
                fh.seek(start_offset)
                chunk = fh.read(end_offset - start_offset)
                transfer_resp = self._http.post(
                    f"{GRAPH_API_BASE}/{self._page_id}/videos",
                    data={
                        "upload_phase": "transfer",
                        "upload_session_id": upload_session_id,
                        "start_offset": start_offset,
                        "access_token": self._page_access_token,
                    },
                    files={"video_file_chunk": chunk},
                    timeout=RESUMABLE_UPLOAD_TRANSFER_TIMEOUT_SECONDS,
                )
                transfer_resp.raise_for_status()
                transfer_data = transfer_resp.json()
                start_offset = int(transfer_data["start_offset"])
                end_offset = int(transfer_data["end_offset"])

        finish_resp = self._http.post(
            f"{GRAPH_API_BASE}/{self._page_id}/videos",
            data={
                "upload_phase": "finish",
                "upload_session_id": upload_session_id,
                "access_token": self._page_access_token,
                "title": metadata.title,
                "description": metadata.description,
                **self._privacy_params(metadata.privacy),
            },
        )
        finish_resp.raise_for_status()
        return video_id

    def _upload_reel(self, video: VideoFile, metadata: PlatformMetadata) -> str:
        """Page Reels publishing (developers.facebook.com/docs/video-api/
        guides/reels-publishing): start -> rupload transfer -> finish.
        Reels have no published=false/secret mode, so anything other than
        "public" is finished as a DRAFT: unpublished, listed in Meta
        Business Suite for the Page's admins, and -- confirmed live
        2026-10-14 -- not viewable logged out via its /reel/ permalink."""
        start_resp = self._http.post(
            f"{GRAPH_API_BASE}/{self._page_id}/video_reels",
            data={"upload_phase": "start", "access_token": self._page_access_token},
        )
        if start_resp.status_code != 200:
            raise FacebookUploadRejectedError("start", start_resp.status_code, start_resp.text)
        start_data = start_resp.json()
        video_id = start_data["video_id"]
        upload_url = start_data.get("upload_url") or f"{REELS_RUPLOAD_BASE}/{video_id}"

        with video.path.open("rb") as fh:
            offset = 0
            while offset < video.size_bytes:
                chunk = fh.read(REELS_UPLOAD_CHUNK_SIZE_BYTES)
                resp = self._http.post(
                    upload_url,
                    headers={
                        "Authorization": f"OAuth {self._page_access_token}",
                        "offset": str(offset),
                        "file_size": str(video.size_bytes),
                    },
                    content=chunk,
                    timeout=RESUMABLE_UPLOAD_TRANSFER_TIMEOUT_SECONDS,
                )
                if resp.status_code not in (200, 206):
                    raise FacebookUploadRejectedError("upload", resp.status_code, resp.text)
                offset += len(chunk)

        finish_resp = self._http.post(
            f"{GRAPH_API_BASE}/{self._page_id}/video_reels",
            data={
                "upload_phase": "finish",
                "video_id": video_id,
                "video_state": "PUBLISHED" if metadata.privacy == "public" else "DRAFT",
                "title": metadata.title,
                "description": metadata.description,
                "access_token": self._page_access_token,
            },
        )
        if finish_resp.status_code != 200:
            raise FacebookUploadRejectedError("finish", finish_resp.status_code, finish_resp.text)
        return video_id

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
