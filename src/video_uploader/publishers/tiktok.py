from __future__ import annotations

import mimetypes

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

PLATFORM = "tiktok"
API_BASE = "https://open.tiktokapis.com/v2"
AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"

# Direct posting (publish immediately), not video.upload -- video.upload
# only drops a draft into the user's inbox, requiring them to finish
# posting by hand in the TikTok app, which doesn't fit this tool's
# "publish now" model.
SCOPES = "video.publish"

# DESIGN.md §3/§8: unaudited apps can only post as private/self-view, and
# this tool ships with that limitation rather than pursuing app audit.
# Always used regardless of metadata.privacy -- same reasoning as
# instagram.py ignoring privacy, for a different platform-specific reason.
PRIVACY_LEVEL = "SELF_ONLY"

MAX_TITLE_LEN = 2200  # TikTok's limit is UTF-16 code units; approximated with len()


class TikTokAuthError(RuntimeError):
    """Raised when no stored TikTok credentials exist yet."""


class TikTokProcessingError(RuntimeError):
    """Raised when creator_info or a published post reports a failure."""


def complete_oauth(code: str, redirect_uri: str) -> dict:
    """Exchange an OAuth authorization `code` for an access/refresh token
    pair and save it under token_store's "tiktok" key. Returns that dict.

    Plain function (not a method) so it can be registered in
    oauth_https_catcher.CALLBACK_ROUTES, same shape as
    facebook.complete_oauth.
    """
    config = load_config()
    platform_config = config.platforms.get(PLATFORM, {})
    client_key = platform_config.get("client_key", "")
    client_secret = platform_config.get("client_secret", "")

    with httpx.Client(timeout=30.0) as client:
        token_resp = client.post(
            f"{API_BASE}/oauth/token/",
            data={
                "client_key": client_key,
                "client_secret": client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        token_resp.raise_for_status()
        payload = token_resp.json()

    token = {
        "access_token": payload["access_token"],
        "refresh_token": payload["refresh_token"],
        "open_id": payload["open_id"],
    }
    token_store.save_token(PLATFORM, token)
    return token


class TikTokPublisher:
    def __init__(self):
        config = load_config()
        platform_config = config.platforms.get(PLATFORM, {})
        self._client_key = platform_config.get("client_key", "")
        self._client_secret = platform_config.get("client_secret", "")
        self._access_token: str | None = None
        self._http: httpx.Client | None = None

    def authenticate(self) -> None:
        token = token_store.load_token(PLATFORM)
        if not token or not token.get("refresh_token"):
            raise TikTokAuthError(
                "No stored TikTok credentials -- connect TikTok via /settings first"
            )

        # Access tokens are only valid for 24 hours (refresh tokens for
        # 365 days) -- always refresh rather than trusting a possibly-
        # stale cached access token, same pattern as youtube.py.
        with httpx.Client(timeout=30.0) as client:
            refresh_resp = client.post(
                f"{API_BASE}/oauth/token/",
                data={
                    "client_key": self._client_key,
                    "client_secret": self._client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": token["refresh_token"],
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            refresh_resp.raise_for_status()
            payload = refresh_resp.json()

        self._access_token = payload["access_token"]
        # TikTok's docs warn the returned refresh_token may differ from
        # the one sent -- always persist whatever comes back.
        token_store.save_token(
            PLATFORM,
            {
                "access_token": payload["access_token"],
                "refresh_token": payload["refresh_token"],
                "open_id": token["open_id"],
            },
        )
        self._http = httpx.Client(timeout=30.0)

    def validate(self, video: VideoFile, metadata: PlatformMetadata) -> list[str]:
        errors: list[str] = []

        if not video.path.exists() or video.size_bytes <= 0:
            errors.append(f"Video file not found or empty: {video.path}")

        if not metadata.title.strip():
            errors.append("Title is required")
        elif len(metadata.title) > MAX_TITLE_LEN:
            errors.append(f"Title exceeds TikTok's {MAX_TITLE_LEN}-character limit")

        # metadata.privacy is intentionally not validated here -- TikTok
        # uploads are always private/self-view regardless of what's
        # requested (DESIGN.md §3/§8), so there's nothing to check.

        if metadata.thumbnail_path:
            # TikTok's only cover mechanism is video_cover_timestamp_ms --
            # a millisecond offset selecting a frame from the uploaded
            # video itself, not an external image upload. Rejected
            # outright rather than silently ignored.
            errors.append(
                "TikTok's API doesn't support custom thumbnail images -- only frame "
                "selection from the uploaded video itself, which this tool doesn't "
                "yet expose -- leave the thumbnail blank for TikTok"
            )

        errors.extend(preflight.check_preflight(video, PLATFORM))

        return errors

    def _auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {self._access_token}"}

    def upload(self, video: VideoFile, metadata: PlatformMetadata) -> JobHandle:
        creator_resp = self._http.post(
            f"{API_BASE}/post/publish/creator_info/query/",
            headers=self._auth_headers(),
        )
        creator_resp.raise_for_status()
        privacy_options = creator_resp.json().get("data", {}).get("privacy_level_options", [])
        if PRIVACY_LEVEL not in privacy_options:
            raise TikTokProcessingError(
                f"This TikTok account/app can't post as {PRIVACY_LEVEL!r} -- "
                f"available options: {privacy_options}"
            )

        content_type = mimetypes.guess_type(str(video.path))[0] or "video/mp4"
        init_resp = self._http.post(
            f"{API_BASE}/post/publish/video/init/",
            headers=self._auth_headers(),
            json={
                "post_info": {
                    "title": metadata.title,
                    "privacy_level": PRIVACY_LEVEL,
                },
                "source_info": {
                    "source": "FILE_UPLOAD",
                    "video_size": video.size_bytes,
                    "chunk_size": video.size_bytes,
                    "total_chunk_count": 1,
                },
            },
        )
        init_resp.raise_for_status()
        init_data = init_resp.json()["data"]
        publish_id = init_data["publish_id"]
        upload_url = init_data["upload_url"]

        try:
            with video.path.open("rb") as fh:
                upload_resp = self._http.put(
                    upload_url,
                    headers={
                        "Content-Type": content_type,
                        "Content-Length": str(video.size_bytes),
                        "Content-Range": f"bytes 0-{video.size_bytes - 1}/{video.size_bytes}",
                    },
                    content=fh.read(),
                )
            upload_resp.raise_for_status()
        except (TimeoutError, ConnectionError, httpx.TimeoutException):
            # publish_id already exists (from init/, before the transfer),
            # so recovery is simpler than Facebook's/YouTube's title-search
            # fallback: just check whether TikTok actually received
            # anything under this publish_id.
            status_resp = self._http.post(
                f"{API_BASE}/post/publish/status/fetch/",
                headers=self._auth_headers(),
                json={"publish_id": publish_id},
            )
            if status_resp.status_code != 200:
                raise
            status = status_resp.json().get("data", {}).get("status")
            if not status:
                raise

        return JobHandle(platform_job_id=-1, platform_native_id=publish_id)

    def get_status(self, job: JobHandle) -> JobStatus:
        response = self._http.post(
            f"{API_BASE}/post/publish/status/fetch/",
            headers=self._auth_headers(),
            json={"publish_id": job.platform_native_id},
        )
        response.raise_for_status()
        data = response.json().get("data", {})
        status = data.get("status")

        if status == "PUBLISH_COMPLETE":
            return JobStatus(status=PlatformJobStatus.PUBLISHED)
        if status in ("PROCESSING_UPLOAD", "PROCESSING_DOWNLOAD"):
            return JobStatus(status=PlatformJobStatus.PROCESSING)
        if status == "FAILED":
            return JobStatus(
                status=PlatformJobStatus.FAILED,
                error_message=data.get("fail_reason") or "TikTok processing error",
            )
        # SEND_TO_USER_INBOX shouldn't occur for direct-post (that's the
        # video.upload/draft path this tool doesn't use) but mapped
        # defensively rather than crashing on an unrecognized value, same
        # as facebook.py's get_status().
        return JobStatus(
            status=PlatformJobStatus.PROCESSING,
            error_message=f"Unrecognized status: {status}",
        )
