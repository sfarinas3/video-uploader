from video_uploader import runtime_settings


def test_set_then_get_round_trips(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_settings, "_RETENTION_OVERRIDE_PATH", tmp_path / "retention.txt")

    result = runtime_settings.set_retention_days(45)

    assert result == 45
    assert runtime_settings.get_retention_days() == 45


def test_set_clamps_to_max(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_settings, "_RETENTION_OVERRIDE_PATH", tmp_path / "retention.txt")

    assert runtime_settings.set_retention_days(9999) == 365


def test_set_allows_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_settings, "_RETENTION_OVERRIDE_PATH", tmp_path / "retention.txt")

    assert runtime_settings.set_retention_days(0) == 0
    assert runtime_settings.get_retention_days() == 0


def test_set_floors_negative_at_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_settings, "_RETENTION_OVERRIDE_PATH", tmp_path / "retention.txt")

    assert runtime_settings.set_retention_days(-10) == 0


class _FakeStorageConfig:
    job_history_retention_days = 17


class _FakeAppConfig:
    storage = _FakeStorageConfig()


def test_get_falls_back_to_config_value_when_no_override_set(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_settings, "_RETENTION_OVERRIDE_PATH", tmp_path / "retention.txt")
    monkeypatch.setattr(runtime_settings, "load_config", lambda: _FakeAppConfig())

    assert runtime_settings.get_retention_days() == 17


def test_get_falls_back_to_config_value_when_sidecar_file_is_corrupt(tmp_path, monkeypatch):
    override_path = tmp_path / "retention.txt"
    override_path.write_text("not-a-number")
    monkeypatch.setattr(runtime_settings, "_RETENTION_OVERRIDE_PATH", override_path)
    monkeypatch.setattr(runtime_settings, "load_config", lambda: _FakeAppConfig())

    assert runtime_settings.get_retention_days() == 17
