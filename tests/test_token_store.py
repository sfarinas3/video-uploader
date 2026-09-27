from video_uploader import token_store


def test_save_and_load_token():
    token_store.save_token("youtube", {"token": "abc", "refresh_token": "xyz"})
    assert token_store.load_token("youtube") == {"token": "abc", "refresh_token": "xyz"}


def test_load_token_missing_returns_none():
    assert token_store.load_token("facebook") is None


def test_delete_token():
    token_store.save_token("youtube", {"token": "abc"})
    token_store.delete_token("youtube")
    assert token_store.load_token("youtube") is None


def test_delete_token_missing_is_a_noop():
    token_store.delete_token("tiktok")
