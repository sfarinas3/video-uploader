"""Live smoke test for the YouTube publisher, per docs/DESIGN.md §10.

Uploads a short, clearly-labeled test video as PRIVATE, verifies it
processed successfully, then deletes it. Never leaves anything live.
Requires YouTube to already be connected via the app's /settings page.

Usage:
    python scripts/youtube_smoke_test.py [path/to/video.mp4]

If no video path is given, a tiny clip is generated with ffmpeg into a
temp directory.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from video_uploader.core.types import PlatformJobStatus, PlatformMetadata, VideoFile  # noqa: E402
from video_uploader.publishers.youtube import YouTubePublisher  # noqa: E402

TEST_TITLE = "[TEST - safe to delete] video-uploader YouTube smoke test"
TEST_DESCRIPTION = (
    "Automated live smoke test upload for the video-uploader project. "
    "Safe to delete -- this is deleted automatically at the end of the test."
)
POLL_INTERVAL_SECONDS = 5
POLL_TIMEOUT_SECONDS = 180


def _generate_test_clip(dest: Path) -> Path:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=3:size=320x240:rate=10",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=3",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            "-pix_fmt",
            "yuv420p",
            str(dest),
        ],
        check=True,
        capture_output=True,
    )
    return dest


def main() -> int:
    if len(sys.argv) > 1:
        video_path = Path(sys.argv[1])
        if not video_path.exists():
            print(f"Video file not found: {video_path}")
            return 1
    else:
        tmp_dir = Path(tempfile.mkdtemp(prefix="video-uploader-smoke-"))
        video_path = _generate_test_clip(tmp_dir / "test-upload-do-not-use.mp4")
        print(f"Generated test clip: {video_path}")

    video = VideoFile(path=video_path, size_bytes=video_path.stat().st_size)
    # Force private unconditionally regardless of any argument this script
    # might grow later -- a live smoke test must never publish anything
    # publicly visible (DESIGN.md §10.2).
    metadata = PlatformMetadata(
        title=TEST_TITLE,
        description=TEST_DESCRIPTION,
        tags=["test"],
        privacy="private",
    )

    publisher = YouTubePublisher()
    print("Authenticating...")
    publisher.authenticate()

    errors = publisher.validate(video, metadata)
    if errors:
        print("Validation failed:")
        for error in errors:
            print(f"  - {error}")
        return 1

    print("Uploading...")
    handle = publisher.upload(video, metadata)
    video_id = handle.platform_native_id
    print(f"Uploaded. YouTube video id: {video_id}")
    print(f"  https://studio.youtube.com/video/{video_id}/edit")

    print("Polling for processing to complete...")
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    status = None
    while time.monotonic() < deadline:
        status = publisher.get_status(handle)
        print(f"  status: {status.status}")
        if status.status in (PlatformJobStatus.PUBLISHED, PlatformJobStatus.FAILED):
            break
        time.sleep(POLL_INTERVAL_SECONDS)

    if status is None or status.status != PlatformJobStatus.PUBLISHED:
        print(f"Upload did not reach 'published' state: {status}")
        print("Deleting test upload anyway...")
        publisher.delete(video_id)
        print("Deleted.")
        return 1

    print("Upload verified as published. Deleting test upload...")
    publisher.delete(video_id)
    print(f"Deleted video {video_id}. Confirm removal in YouTube Studio if in doubt.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
