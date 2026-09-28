from video_uploader import oauth_https_catcher
from video_uploader.publishers.facebook import complete_oauth as facebook_complete_oauth
from video_uploader.publishers.tiktok import complete_oauth as tiktok_complete_oauth


def test_callback_routes_dispatch_facebook_and_tiktok_to_the_right_handler():
    routes = oauth_https_catcher.CALLBACK_ROUTES
    assert routes[oauth_https_catcher.FACEBOOK_CALLBACK_PATH] is facebook_complete_oauth
    assert routes[oauth_https_catcher.TIKTOK_CALLBACK_PATH] is tiktok_complete_oauth


def test_callback_urls_are_built_from_their_own_paths():
    assert oauth_https_catcher.CALLBACK_URL.endswith(oauth_https_catcher.FACEBOOK_CALLBACK_PATH)
    assert oauth_https_catcher.TIKTOK_CALLBACK_URL.endswith(
        oauth_https_catcher.TIKTOK_CALLBACK_PATH
    )
    assert oauth_https_catcher.OAUTH_DOMAIN in oauth_https_catcher.CALLBACK_URL
    assert oauth_https_catcher.OAUTH_DOMAIN in oauth_https_catcher.TIKTOK_CALLBACK_URL
