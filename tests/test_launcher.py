from video_uploader import launcher


def test_returns_none_when_default_config_exists(tmp_path, monkeypatch):
    default_config = tmp_path / "config.yaml"
    default_config.write_text("server: {}")
    monkeypatch.setattr(launcher, "DEFAULT_CONFIG_PATH", default_config)
    monkeypatch.setattr(launcher, "POINTER_PATH", tmp_path / "config_path.txt")

    assert launcher.resolve_config_path() is None


def test_uses_remembered_pointer_without_prompting(tmp_path, monkeypatch):
    remembered = tmp_path / "elsewhere" / "config.yaml"
    remembered.parent.mkdir()
    remembered.write_text("server: {}")
    pointer = tmp_path / "config_path.txt"
    pointer.write_text(str(remembered))
    monkeypatch.setattr(launcher, "DEFAULT_CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(launcher, "POINTER_PATH", pointer)
    monkeypatch.setattr(
        launcher, "_prompt_for_config_file", lambda: (_ for _ in ()).throw(AssertionError("should not prompt"))
    )

    assert launcher.resolve_config_path() == remembered


def test_stale_pointer_is_cleaned_up_and_falls_back_to_default(tmp_path, monkeypatch):
    default_config = tmp_path / "config.yaml"
    default_config.write_text("server: {}")
    pointer = tmp_path / "config_path.txt"
    pointer.write_text(str(tmp_path / "gone.yaml"))
    monkeypatch.setattr(launcher, "DEFAULT_CONFIG_PATH", default_config)
    monkeypatch.setattr(launcher, "POINTER_PATH", pointer)

    assert launcher.resolve_config_path() is None
    assert not pointer.exists()


def test_prompts_and_remembers_choice_when_nothing_exists_yet(tmp_path, monkeypatch):
    chosen = tmp_path / "backup-config.yaml"
    chosen.write_text("server: {}")
    pointer = tmp_path / "config_path.txt"
    monkeypatch.setattr(launcher, "DEFAULT_CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(launcher, "POINTER_PATH", pointer)
    monkeypatch.setattr(launcher, "_prompt_for_config_file", lambda: chosen)

    result = launcher.resolve_config_path()

    assert result == chosen
    assert pointer.read_text() == str(chosen)


def test_prompt_cancelled_returns_none_and_writes_no_pointer(tmp_path, monkeypatch):
    pointer = tmp_path / "config_path.txt"
    monkeypatch.setattr(launcher, "DEFAULT_CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(launcher, "POINTER_PATH", pointer)
    monkeypatch.setattr(launcher, "_prompt_for_config_file", lambda: None)

    assert launcher.resolve_config_path() is None
    assert not pointer.exists()
