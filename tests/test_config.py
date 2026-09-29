import pytest

from video_uploader.config import (
    JOB_HISTORY_RETENTION_DAYS_DEFAULT,
    JOB_HISTORY_RETENTION_DAYS_MAX,
    JOB_HISTORY_RETENTION_DAYS_MIN,
    _clamped_retention_days,
    load_config,
)


def test_clamped_retention_days_passes_through_valid_value():
    assert _clamped_retention_days(90) == 90


def test_clamped_retention_days_caps_at_one_year():
    assert _clamped_retention_days(1000) == JOB_HISTORY_RETENTION_DAYS_MAX


def test_clamped_retention_days_allows_zero():
    assert _clamped_retention_days(0) == 0


def test_clamped_retention_days_floors_at_zero():
    assert _clamped_retention_days(-5) == JOB_HISTORY_RETENTION_DAYS_MIN


@pytest.mark.parametrize("bad_value", ["not-a-number", None, ""])
def test_clamped_retention_days_falls_back_to_default_for_invalid_input(bad_value):
    assert _clamped_retention_days(bad_value) == JOB_HISTORY_RETENTION_DAYS_DEFAULT


def test_clamped_retention_days_accepts_numeric_strings_from_yaml():
    assert _clamped_retention_days("45") == 45


def test_load_config_defaults_retention_when_key_absent(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("server: {}\nstorage: {}\nplatforms: {}\n")
    config = load_config(config_path)
    assert config.storage.job_history_retention_days == JOB_HISTORY_RETENTION_DAYS_DEFAULT


def test_load_config_reads_and_clamps_retention_from_yaml(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("storage:\n  job_history_retention_days: 9999\n")
    config = load_config(config_path)
    assert config.storage.job_history_retention_days == JOB_HISTORY_RETENTION_DAYS_MAX
