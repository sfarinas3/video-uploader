"""Live smoke test for the Instagram publisher, per docs/DESIGN.md §10.

Uploads a short, clearly-labeled test video to Instagram (Business account
linked via the connected Facebook Page), verifies it published, then
BLOCKS waiting for you to delete it by hand -- Instagram's Content
Publishing API has no delete-published-media endpoint (DESIGN.md §10.5),
so this is the required fallback. The test is not considered finished,
pass or fail, until you confirm deletion. Requires Instagram to already be
connected via the app's /settings page (which happens automatically when
Facebook is connected, if the Page has a linked Instagram Business
account).

Usage:
    python scripts/instagram_smoke_test.py [path/to/video.mp4]

If no video path is given, a tiny clip is generated with ffmpeg into a
temp directory.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from video_uploader.core.types import PlatformJobStatus, PlatformMetadata, VideoFile  # noqa: E402
from video_uploader.publishers.instagram import InstagramPublisher  # noqa: E402

TEST_TITLE = "[TEST - safe to delete] video-uploader Instagram smoke test"
TEST_DESCRIPTION = (
    "Automated live smoke test upload for the video-uploader project. "
    "Safe to delete -- please delete this manually once this script asks you to."
)


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
    # Force the feed variant unconditionally regardless of any argument
    # this script might grow later -- least prominent placement Instagram
    # offers (not Reels/Stories, which surface more aggressively in
    # discovery). Privacy is accepted but ignored by InstagramPublisher --
    # Instagram Business content has no privacy tiers at all (DESIGN.md
    # §10.2).
    metadata = PlatformMetadata(
        title=TEST_TITLE,
        description=TEST_DESCRIPTION,
        tags=["test"],
        format_variant="feed",
    )

    publisher = InstagramPublisher()
    print("Authenticating...")
    publisher.authenticate()

    errors = publisher.validate(video, metadata)
    if errors:
        print("Validation failed:")
        for error in errors:
            print(f"  - {error}")
        return 1

    print("Uploading (this blocks until Instagram finishes processing and publishes it)...")
    handle = publisher.upload(video, metadata)
    media_id = handle.platform_native_id
    print(f"Published. Instagram media id: {media_id}")

    status = publisher.get_status(handle)
    if status.status != PlatformJobStatus.PUBLISHED:
        print(f"Upload did not report as published: {status}")
        return 1

    permalink = publisher.get_permalink(media_id)

    print()
    print("=" * 70)
    print("Instagram has no API to delete published media. You must delete")
    print("this test post by hand, right now:")
    print()
    print(f"    {permalink}")
    print()
    print("Open that link, delete the post, then confirm below.")
    print("=" * 70)
    confirmation = input("Type 'deleted' once you've removed it: ").strip().lower()
    if confirmation != "deleted":
        print("Not confirmed as deleted. Go delete it, then re-run to confirm -- "
              f"the post is still live at {permalink}")
        return 1

    print("Confirmed. Test complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
