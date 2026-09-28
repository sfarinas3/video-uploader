from video_uploader import launcher


def _patch_paths(monkeypatch, tmp_path, example_content="server: {}\n"):
    example = tmp_path / "config.example.yaml"
    example.write_text(example_content)
    pointer = tmp_path / "config_path.txt"
    monkeypatch.setattr(launcher, "EXAMPLE_CONFIG_PATH", example)
    monkeypatch.setattr(launcher, "POINTER_PATH", pointer)
    return example, pointer


def test_uses_remembered_pointer_without_prompting(tmp_path, monkeypatch):
    _, pointer = _patch_paths(monkeypatch, tmp_path)
    remembered = tmp_path / "elsewhere" / "config.yaml"
    remembered.parent.mkdir()
    remembered.write_text("server: {}")
    pointer.write_text(str(remembered))
    monkeypatch.setattr(
        launcher, "_prompt_for_config_file", lambda: (_ for _ in ()).throw(AssertionError("should not prompt"))
    )

    assert launcher.resolve_config_path() == remembered


def test_stale_pointer_is_cleaned_up_and_reprompts(tmp_path, monkeypatch):
    _, pointer = _patch_paths(monkeypatch, tmp_path)
    pointer.write_text(str(tmp_path / "gone.yaml"))
    chosen = tmp_path / "new-config.yaml"
    chosen.write_text("server: {}")
    monkeypatch.setattr(launcher, "_prompt_for_config_file", lambda: chosen)

    assert launcher.resolve_config_path() == chosen
    assert pointer.read_text() == str(chosen)


def test_no_pointer_always_prompts_even_if_a_file_sits_next_to_the_app(tmp_path, monkeypatch):
    """There's no implicit default -- an unselected config.yaml sitting
    right next to the app must never be picked up automatically."""
    _, pointer = _patch_paths(monkeypatch, tmp_path)
    (tmp_path / "config.yaml").write_text("server: {}")  # present, but never chosen
    chosen = tmp_path / "picked.yaml"
    chosen.write_text("server: {}")
    prompted = {"called": False}

    def fake_prompt():
        prompted["called"] = True
        return chosen

    monkeypatch.setattr(launcher, "_prompt_for_config_file", fake_prompt)

    result = launcher.resolve_config_path()

    assert prompted["called"] is True
    assert result == chosen


def test_cancelled_prompt_falls_back_to_example_without_remembering(tmp_path, monkeypatch):
    example, pointer = _patch_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(launcher, "_prompt_for_config_file", lambda: None)

    assert launcher.resolve_config_path() == example
    assert not pointer.exists()


def test_choose_new_config_file_reuses_existing_file_as_is(tmp_path, monkeypatch):
    _, pointer = _patch_paths(monkeypatch, tmp_path)
    existing = tmp_path / "already-set-up.yaml"
    existing.write_text("server: {host: custom}")
    monkeypatch.setattr(launcher, "_prompt_for_config_file", lambda: existing)

    result = launcher.choose_new_config_file()

    assert result == existing
    assert existing.read_text() == "server: {host: custom}"  # untouched
    assert pointer.read_text() == str(existing)


def test_choose_new_config_file_seeds_a_brand_new_file_from_example(tmp_path, monkeypatch):
    example, pointer = _patch_paths(monkeypatch, tmp_path, example_content="server: {port: 8000}\n")
    new_location = tmp_path / "somewhere" / "config.yaml"
    monkeypatch.setattr(launcher, "_prompt_for_config_file", lambda: new_location)

    result = launcher.choose_new_config_file()

    assert result == new_location
    assert new_location.read_text() == example.read_text()
    assert pointer.read_text() == str(new_location)


def test_choose_new_config_file_cancelled_leaves_existing_pointer_untouched(tmp_path, monkeypatch):
    _, pointer = _patch_paths(monkeypatch, tmp_path)
    pointer.write_text(str(tmp_path / "still-active.yaml"))
    monkeypatch.setattr(launcher, "_prompt_for_config_file", lambda: None)

    result = launcher.choose_new_config_file()

    assert result is None
    assert pointer.read_text() == str(tmp_path / "still-active.yaml")
