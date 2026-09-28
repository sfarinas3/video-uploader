from __future__ import annotations

from datetime import datetime, timedelta, timezone

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from video_uploader import preflight, token_store
from video_uploader.config import load_config
from video_uploader.core.types import (
    JobHandle,
    JobStatus,
    PlatformJobStatus,
    PlatformMetadata,
    VideoFile,
)

PLATFORM = "youtube"
SCOPES = ["https://www.googleapis.com/auth/youtube"]
TOKEN_URI = "https://oauth2.googleapis.com/token"

# YouTube Data API v3 metadata limits.
MAX_TITLE_LEN = 100
MAX_DESCRIPTION_LEN = 5000
MAX_TAGS_LEN = 500  # combined character length of all tags

# A read timeout waiting for insert()'s response doesn't tell us whether
# YouTube actually created the video before the connection dropped -- it
# often does. Treating that as a hard failure risks both permanently
# orphaning a real (private) upload and creating a duplicate on retry. This
# is how far back to look for a same-titled upload before giving up and
# reporting a real failure.
TIMEOUT_RECOVERY_WINDOW_MINUTES = 10


class YouTubeAuthError(RuntimeError):
    """Raised when no stored YouTube credentials exist yet."""


class YouTubePublisher:
    def __init__(self):
        config = load_config()
        platform_config = config.platforms.get(PLATFORM, {})
        self._client_id = platform_config.get("client_id", "")
        self._client_secret = platform_config.get("client_secret", "")
        self._youtube = None

    def authenticate(self) -> None:
        token = token_store.load_token(PLATFORM)
        if not token or not token.get("refresh_token"):
            raise YouTubeAuthError(
                "No stored YouTube credentials -- connect YouTube via /settings first"
            )

        credentials = Credentials(
            token=token.get("token"),
            refresh_token=token["refresh_token"],
            token_uri=TOKEN_URI,
            client_id=self._client_id,
            client_secret=self._client_secret,
            scopes=token.get("scopes", SCOPES),
        )
        # Always refresh rather than trusting a possibly-stale cached
        # access token -- simpler and more robust than tracking expiry.
        credentials.refresh(Request())
        token_store.save_token(
            PLATFORM,
            {
                "token": credentials.token,
                "refresh_token": credentials.refresh_token,
                "scopes": list(credentials.scopes or SCOPES),
            },
        )
        self._youtube = build("youtube", "v3", credentials=credentials)

    def validate(self, video: VideoFile, metadata: PlatformMetadata) -> list[str]:
        errors: list[str] = []

        if not video.path.exists() or video.size_bytes <= 0:
            errors.append(f"Video file not found or empty: {video.path}")

        if not metadata.title.strip():
            errors.append("Title is required")
        elif len(metadata.title) > MAX_TITLE_LEN:
            errors.append(f"Title exceeds YouTube's {MAX_TITLE_LEN}-character limit")

        if len(metadata.description) > MAX_DESCRIPTION_LEN:
            errors.append(f"Description exceeds YouTube's {MAX_DESCRIPTION_LEN}-character limit")

        if sum(len(tag) for tag in metadata.tags) > MAX_TAGS_LEN:
            errors.append(f"Tags exceed YouTube's combined {MAX_TAGS_LEN}-character limit")

        if metadata.thumbnail_path and not metadata.thumbnail_path.exists():
            errors.append(f"Thumbnail file not found: {metadata.thumbnail_path}")

        if metadata.privacy not in ("private", "unlisted", "public"):
            errors.append(f"Invalid privacy value: {metadata.privacy!r}")

        errors.extend(preflight.check_preflight(video, PLATFORM))

        return errors

    def upload(self, video: VideoFile, metadata: PlatformMetadata) -> JobHandle:
        body = {
            "snippet": {
                "title": metadata.title,
                "description": metadata.description,
                "tags": metadata.tags,
            },
            "status": {"privacyStatus": metadata.privacy},
        }
        media = MediaFileUpload(str(video.path), chunksize=-1, resumable=True)
        try:
            response = (
                self._youtube.videos()
                .insert(part="snippet,status", body=body, media_body=media)
                .execute()
            )
            video_id = response["id"]
        except (TimeoutError, ConnectionError):
            # Ambiguous: the request may have completed on YouTube's side
            # before the response reached us. Check for it before
            # reporting a failure that would invite a duplicate on retry.
            recovered_id = self._find_recently_uploaded_video(metadata.title)
            if recovered_id is None:
                raise
            video_id = recovered_id

        if metadata.thumbnail_path:
            self._set_thumbnail_best_effort(video_id, metadata.thumbnail_path)

        return JobHandle(platform_job_id=-1, platform_native_id=video_id)

    def _set_thumbnail_best_effort(self, video_id: str, thumbnail_path) -> None:
        """The video itself is already uploaded by the time this runs --
        a thumbnail failure here must never fail the whole platform job
        (that would mark it FAILED without ever recording video_id,
        orphaning an actually-successful upload, the same failure mode
        the timeout-recovery logic above exists to avoid). Thumbnails are
        explicitly optional (DESIGN.md §6.4); the video publish is not."""
        try:
            self._youtube.thumbnails().set(
                videoId=video_id, media_body=MediaFileUpload(str(thumbnail_path))
            ).execute()
        except Exception:  # noqa: BLE001 - best-effort, see docstring
            pass

    def _find_recently_uploaded_video(self, title: str) -> str | None:
        channel_resp = self._youtube.channels().list(part="contentDetails", mine=True).execute()
        items = channel_resp.get("items", [])
        if not items:
            return None
        uploads_playlist_id = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]

        playlist_resp = (
            self._youtube.playlistItems()
            .list(part="snippet", playlistId=uploads_playlist_id, maxResults=5)
            .execute()
        )
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=TIMEOUT_RECOVERY_WINDOW_MINUTES)
        for item in playlist_resp.get("items", []):
            snippet = item["snippet"]
            if snippet.get("title") != title:
                continue
            published_at = datetime.fromisoformat(snippet["publishedAt"].replace("Z", "+00:00"))
            if published_at >= cutoff:
                return snippet["resourceId"]["videoId"]
        return None

    def get_status(self, job: JobHandle) -> JobStatus:
        response = (
            self._youtube.videos().list(part="status", id=job.platform_native_id).execute()
        )
        items = response.get("items", [])
        if not items:
            return JobStatus(
                status=PlatformJobStatus.FAILED,
                error_message=f"Video {job.platform_native_id} not found on YouTube",
            )

        status = items[0]["status"]
        upload_status = status.get("uploadStatus")

        if upload_status == "processed":
            return JobStatus(status=PlatformJobStatus.PUBLISHED)
        if upload_status == "uploaded":
            return JobStatus(status=PlatformJobStatus.PROCESSING)
        if upload_status in ("rejected", "failed"):
            reason = status.get("rejectionReason") or status.get("failureReason") or upload_status
            return JobStatus(status=PlatformJobStatus.FAILED, error_message=reason)
        return JobStatus(
            status=PlatformJobStatus.PROCESSING,
            error_message=f"Unrecognized uploadStatus: {upload_status}",
        )

    def delete(self, platform_native_id: str) -> None:
        """Not part of the Publisher protocol. Used by the live smoke
        test's mandatory cleanup step (DESIGN.md §10) and by any future
        "delete a published job" UI action."""
        self._youtube.videos().delete(id=platform_native_id).execute()

    def get_connected_channel_name(self) -> str | None:
        """Not part of the Publisher protocol. A Google account can manage
        several YouTube channels (brand accounts); the OAuth token is tied
        to whichever channel was active during consent, not necessarily
        the one the user meant. Used by /settings to show the actual
        connected channel instead of leaving that as an assumption."""
        response = self._youtube.channels().list(part="snippet", mine=True).execute()
        items = response.get("items", [])
        return items[0]["snippet"]["title"] if items else None

    def find_top_tags(self, keyword: str, max_results: int = 15) -> list[tuple[str, int]]:
        """Not part of the Publisher protocol (DESIGN.md milestone 10).
        Finds the top-ranking videos for `keyword` and returns the tags
        they use, aggregated and sorted by how many of those videos use
        each one (descending). Confirmed live: videos.list returns
        snippet.tags for any public video, not just ones this account
        owns -- this is real competitive-tag data, not a guess.

        search.list costs 100 quota units per call regardless of
        max_results -- callers should surface that cost, not hide it."""
        search_response = (
            self._youtube.search()
            .list(part="snippet", q=keyword, type="video", order="relevance", maxResults=max_results)
            .execute()
        )
        video_ids = [item["id"]["videoId"] for item in search_response.get("items", [])]
        if not video_ids:
            return []

        videos_response = (
            self._youtube.videos().list(part="snippet", id=",".join(video_ids)).execute()
        )
        tag_counts: dict[str, int] = {}
        for item in videos_response.get("items", []):
            for tag in item["snippet"].get("tags", []):
                tag_counts[tag] = tag_counts.get(tag, 0) + 1

        return sorted(tag_counts.items(), key=lambda kv: kv[1], reverse=True)
