"""Live smoke test for the TikTok publisher, per docs/DESIGN.md §10.

Uploads a short, clearly-labeled test video to TikTok. Always posts as
private/self-view -- unaudited apps can't post publicly at all (DESIGN.md
§3/§8), so there's no override needed the way facebook_smoke_test.py
forces privacy. Verifies it published, then BLOCKS waiting for you to
delete it by hand -- TikTok's Content Posting API has no delete endpoint
(DESIGN.md §10.5). The test is not considered finished, pass or fail,
until you confirm deletion. Requires TikTok to already be connected via
the app's /settings page.

Usage:
    python scripts/tiktok_smoke_test.py [path/to/video.mp4]

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
from video_uploader.publishers.tiktok import TikTokPublisher  # noqa: E402

TEST_TITLE = "[TEST - safe to delete] video-uploader TikTok smoke test"
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
    metadata = PlatformMetadata(title=TEST_TITLE, tags=["test"])

    publisher = TikTokPublisher()
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
    print(f"Uploaded. TikTok publish id: {handle.platform_native_id}")

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
        return 1

    print()
    print("=" * 70)
    print("TikTok has no API to delete published posts. You must delete")
    print("this test post by hand, right now. It was posted as private/")
    print("self-view, so only you can see it -- open the TikTok app, find")
    print("this video on your own profile (look for the title below),")
    print("and delete it:")
    print()
    print(f"    {TEST_TITLE}")
    print()
    print("Then confirm below.")
    print("=" * 70)
    confirmation = input("Type 'deleted' once you've removed it: ").strip().lower()
    if confirmation != "deleted":
        print("Not confirmed as deleted. Go delete it, then re-run to confirm.")
        return 1

    print("Confirmed. Test complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
