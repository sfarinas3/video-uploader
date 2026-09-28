from __future__ import annotations

import time

import httpx

from video_uploader import token_store
from video_uploader.core.types import (
    JobHandle,
    JobStatus,
    PlatformJobStatus,
    PlatformMetadata,
    VideoFile,
)
from video_uploader.publishers.facebook import GRAPH_API_BASE, GRAPH_API_VERSION

PLATFORM = "instagram"

RUPLOAD_BASE = f"https://rupload.facebook.com/ig-api-upload/{GRAPH_API_VERSION}"

# "VIDEO" (plain feed video) is deprecated by Instagram -- confirmed live,
# Meta's API now rejects it with "The VIDEO value for media_type is
# deprecated. Use the REELS media type to publish a video to your
# Instagram feed." So "feed" and "reel" both resolve to REELS; the
# format_variant distinction is kept anyway since it still matters for
# validate() and for callers' own intent even though the API no longer
# does.
FORMAT_TO_MEDIA_TYPE = {"feed": "REELS", "reel": "REELS", "story": "STORIES"}
DEFAULT_MEDIA_TYPE = "REELS"

# How long to wait for a just-uploaded container to finish processing
# before media_publish can even be called -- unlike YouTube/Facebook,
# Instagram won't let us publish (or report any status) until this
# completes, so it has to happen inside upload() itself. See instagram.py's
# design notes in the M5 plan for why.
UPLOAD_PROCESSING_POLL_SECONDS = 5
UPLOAD_PROCESSING_TIMEOUT_SECONDS = 300

# Same reasoning as facebook.py's TIMEOUT_RECOVERY_WINDOW_MINUTES, but only
# applied around the media_publish call -- see upload()'s docstring.
TIMEOUT_RECOVERY_WINDOW_SECONDS = 60


class InstagramAuthError(RuntimeError):
    """Raised when no stored Instagram credentials exist yet."""


class InstagramProcessingError(RuntimeError):
    """Raised when a media container fails or expires before publishing."""


class InstagramPublisher:
    def __init__(self):
        self._ig_user_id: str | None = None
        self._access_token: str | None = None
        self._http: httpx.Client | None = None

    def authenticate(self) -> None:
        token = token_store.load_token(PLATFORM)
        if not token or not token.get("page_access_token"):
            raise InstagramAuthError(
                "No stored Instagram credentials -- connect Facebook via /settings first "
                "(Instagram publishing reuses that connection; the linked Page must have "
                "an Instagram Business account)"
            )
        self._ig_user_id = token["ig_user_id"]
        self._access_token = token["page_access_token"]
        self._http = httpx.Client(timeout=30.0)

    def validate(self, video: VideoFile, metadata: PlatformMetadata) -> list[str]:
        errors: list[str] = []

        if not video.path.exists() or video.size_bytes <= 0:
            errors.append(f"Video file not found or empty: {video.path}")

        if not metadata.title.strip() and not metadata.description.strip():
            errors.append("Title or description is required (used as the caption)")

        if metadata.thumbnail_path and not metadata.thumbnail_path.exists():
            errors.append(f"Thumbnail file not found: {metadata.thumbnail_path}")

        if metadata.format_variant and metadata.format_variant not in FORMAT_TO_MEDIA_TYPE:
            errors.append(f"Invalid format_variant: {metadata.format_variant!r}")

        # Instagram Business content has no privacy tiers at all -- not
        # even Facebook Pages' "secret" pseudo-privacy (DESIGN.md §10.2).
        # metadata.privacy is intentionally accepted without validation
        # here; it's simply not sent to the API.

        return errors

    def _caption(self, metadata: PlatformMetadata) -> str:
        title = metadata.title.strip()
        description = metadata.description.strip()
        if title and description:
            return f"{title}\n\n{description}"
        return description or title

    def upload(self, video: VideoFile, metadata: PlatformMetadata) -> JobHandle:
        media_type = FORMAT_TO_MEDIA_TYPE.get(metadata.format_variant or "", DEFAULT_MEDIA_TYPE)

        container_resp = self._http.post(
            f"{GRAPH_API_BASE}/{self._ig_user_id}/media",
            data={
                "media_type": media_type,
                "upload_type": "resumable",
                "caption": self._caption(metadata),
                "access_token": self._access_token,
            },
        )
        container_resp.raise_for_status()
        container_id = container_resp.json()["id"]

        with video.path.open("rb") as fh:
            upload_resp = self._http.post(
                f"{RUPLOAD_BASE}/{container_id}",
                headers={
                    "Authorization": f"OAuth {self._access_token}",
                    "offset": "0",
                    "file_size": str(video.size_bytes),
                },
                content=fh.read(),
            )
        upload_resp.raise_for_status()

        self._wait_for_container_finished(container_id)

        try:
            publish_resp = self._http.post(
                f"{GRAPH_API_BASE}/{self._ig_user_id}/media_publish",
                data={"creation_id": container_id, "access_token": self._access_token},
            )
            publish_resp.raise_for_status()
            media_id = publish_resp.json()["id"]
        except (TimeoutError, ConnectionError, httpx.TimeoutException):
            # media_publish may have actually succeeded before the client
            # timed out waiting for the response. Only this call gets a
            # recovery check -- container creation/upload timing out means
            # nothing was posted, so re-raising there is already correct.
            media_id = self._recover_published_id(container_id)
            if media_id is None:
                raise

        return JobHandle(platform_job_id=-1, platform_native_id=media_id)

    def _wait_for_container_finished(self, container_id: str) -> None:
        deadline = time.monotonic() + UPLOAD_PROCESSING_TIMEOUT_SECONDS
        while True:
            status_resp = self._http.get(
                f"{GRAPH_API_BASE}/{container_id}",
                params={"fields": "status_code", "access_token": self._access_token},
            )
            status_resp.raise_for_status()
            status_code = status_resp.json().get("status_code")

            if status_code == "FINISHED":
                return
            if status_code in ("ERROR", "EXPIRED"):
                raise InstagramProcessingError(
                    f"Instagram media container {container_id} failed with status {status_code}"
                )
            if time.monotonic() >= deadline:
                raise InstagramProcessingError(
                    f"Instagram media container {container_id} did not finish processing "
                    f"within {UPLOAD_PROCESSING_TIMEOUT_SECONDS}s (last status: {status_code})"
                )
            time.sleep(UPLOAD_PROCESSING_POLL_SECONDS)

    def _recover_published_id(self, container_id: str) -> str | None:
        """Best-effort check for whether media_publish actually went
        through despite a client-side timeout. Meta's docs don't confirm
        whether a published container remains queryable by its original
        container id, so this checks status_code first and falls back to
        None (caller re-raises) rather than assuming -- to be confirmed
        against live behavior."""
        status_resp = self._http.get(
            f"{GRAPH_API_BASE}/{container_id}",
            params={"fields": "status_code", "access_token": self._access_token},
        )
        if status_resp.status_code == 200 and status_resp.json().get("status_code") == "PUBLISHED":
            return container_id
        return None

    def get_status(self, job: JobHandle) -> JobStatus:
        # By the time upload() returns, the post is already published --
        # the container-finished wait and media_publish call both happen
        # inside upload() (Instagram requires that ordering; there's no
        # post to check the status of before then). This is just an
        # existence check.
        response = self._http.get(
            f"{GRAPH_API_BASE}/{job.platform_native_id}",
            params={"fields": "id", "access_token": self._access_token},
        )
        if response.status_code == 404:
            return JobStatus(
                status=PlatformJobStatus.FAILED,
                error_message=f"Media {job.platform_native_id} not found on Instagram",
            )
        response.raise_for_status()
        return JobStatus(status=PlatformJobStatus.PUBLISHED)

    def get_permalink(self, media_id: str) -> str:
        """Not part of the Publisher protocol. Used by the live smoke
        test's mandatory manual-cleanup step (DESIGN.md §10.5) -- Instagram
        has no delete-published-media endpoint, so the smoke test has to
        print this and block on the user deleting it by hand."""
        response = self._http.get(
            f"{GRAPH_API_BASE}/{media_id}",
            params={"fields": "permalink", "access_token": self._access_token},
        )
        response.raise_for_status()
        return response.json()["permalink"]
