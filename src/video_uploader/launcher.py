from __future__ import annotations

import os
from pathlib import Path

from video_uploader.config import DEFAULT_CONFIG_PATH, REPO_ROOT

# Remembers a config.yaml the user picked from somewhere other than the
# default location next to the app, so it doesn't need reselecting on every
# launch (e.g. after reinstalling, or running the app from a second machine
# pointed at the same config file on a synced drive).
POINTER_PATH = REPO_ROOT / "config_path.txt"


def _prompt_for_config_file() -> Path | None:
    """Native file-picker so a first run can reuse an existing config.yaml
    (already has platform credentials filled in) instead of starting from
    a blank config.example.yaml. Returns None if the user cancels."""
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    chosen = filedialog.askopenfilename(
        title="Video Uploader: select an existing config.yaml (Cancel to start fresh)",
        filetypes=[("YAML config", "*.yaml *.yml"), ("All files", "*.*")],
    )
    root.destroy()
    return Path(chosen) if chosen else None


def resolve_config_path() -> Path | None:
    """Figures out which config.yaml this run should use. Returns None to
    mean "use load_config()'s own default logic" (config.yaml next to the
    app if present, else the blank example)."""
    if POINTER_PATH.exists():
        remembered = Path(POINTER_PATH.read_text().strip())
        if remembered.is_file():
            return remembered
        POINTER_PATH.unlink(missing_ok=True)  # stale pointer -- file moved/deleted

    if DEFAULT_CONFIG_PATH.exists():
        return None

    chosen = _prompt_for_config_file()
    if chosen is not None:
        POINTER_PATH.write_text(str(chosen))
        return chosen

    return None


def main() -> None:
    """Console-script / run.py entry point. Must resolve the config path
    and set VIDEO_UPLOADER_CONFIG_PATH *before* importing video_uploader.web.app,
    since that module loads config at import time."""
    config_path = resolve_config_path()
    if config_path is not None:
        os.environ["VIDEO_UPLOADER_CONFIG_PATH"] = str(config_path)

    from video_uploader.web.app import main as run_web_app

    run_web_app()
