from video_uploader import hashtags


def test_extract_finds_hashtags_in_order_without_hash():
    text = "New riff!\n\n#metal #djent some words #SpaceCatCore"
    assert hashtags.extract(text) == ["metal", "djent", "SpaceCatCore"]


def test_extract_dedupes_case_insensitively_keeping_first():
    assert hashtags.extract("#Metal #metal #METAL #guitar") == ["Metal", "guitar"]


def test_extract_ignores_non_hashtags():
    text = "issue#4, see https://x.com/page#section, it&#39;s #1 and ##double and #9strings"
    assert hashtags.extract(text) == ["9strings"]


def test_extract_handles_punctuation_after_tag():
    assert hashtags.extract("Loving this (#guitar). #metal!") == ["guitar", "metal"]


def test_from_phrase_strips_spaces_and_punctuation():
    assert hashtags.from_phrase("metal guitar") == "#metalguitar"
    assert hashtags.from_phrase("rock 'n' roll") == "#rocknroll"
    assert hashtags.from_phrase("2024") == ""
    assert hashtags.from_phrase("!!!") == ""


def test_append_skips_tags_already_present():
    assert hashtags.append("Riff #metal", ["Metal", "djent"]) == "Riff #metal #djent"


def test_append_to_empty_text_has_no_leading_separator():
    assert hashtags.append("", ["a1", "b2"]) == "#a1 #b2"


def test_append_skips_tags_that_would_exceed_max_len():
    assert hashtags.append("Title", ["toolongtag", "ok"], max_len=12) == "Title #ok"
