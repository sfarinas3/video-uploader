from video_uploader.publishers.base import Publisher
from video_uploader.publishers.facebook import FacebookPublisher
from video_uploader.publishers.instagram import InstagramPublisher
from video_uploader.publishers.tiktok import TikTokPublisher
from video_uploader.publishers.youtube import YouTubePublisher

# Registered by each platform's implementation as it's built. A missing
# entry is treated by CoreEngine.run_job() as "not implemented yet" and
# fails only that platform's job, not the whole upload.
PLATFORM_PUBLISHERS: dict[str, type[Publisher]] = {
    "youtube": YouTubePublisher,
    "facebook": FacebookPublisher,
    "instagram": InstagramPublisher,
    "tiktok": TikTokPublisher,
}
