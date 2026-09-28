from __future__ import annotations

import os
from pathlib import Path

from video_uploader.config import EXAMPLE_CONFIG_PATH, REPO_ROOT

# Remembers which config.yaml the user picked, so it doesn't need
# reselecting on every launch. There is deliberately no automatic default
# (e.g. "config.yaml next to the app") -- the only config.yaml this app
# ever uses is one the user explicitly chose at some point, either now or
# on a previous launch.
POINTER_PATH = REPO_ROOT / "config_path.txt"


def _prompt_for_config_file() -> Path | None:
    """Native picker: reuse an existing config.yaml, or create a new one
    at a location the user chooses. Returns None if cancelled entirely."""
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)

    dialog = tk.Toplevel(root)
    dialog.title("Video Uploader")
    dialog.attributes("-topmost", True)
    tk.Label(
        dialog,
        text="Choose where your config.yaml (platform credentials) lives.",
        padx=24,
        pady=12,
    ).pack()

    result: dict[str, str] = {}

    def use_existing() -> None:
        result["path"] = filedialog.askopenfilename(
            parent=dialog,
            title="Select an existing config.yaml",
            filetypes=[("YAML config", "*.yaml *.yml"), ("All files", "*.*")],
        )
        dialog.destroy()

    def create_new() -> None:
        result["path"] = filedialog.asksaveasfilename(
            parent=dialog,
            title="Choose where to create your new config.yaml",
            defaultextension=".yaml",
            initialfile="config.yaml",
            filetypes=[("YAML config", "*.yaml *.yml")],
        )
        dialog.destroy()

    tk.Button(dialog, text="Use an existing config.yaml...", command=use_existing, width=32).pack(
        padx=24, pady=(0, 6)
    )
    tk.Button(dialog, text="Create a new config.yaml...", command=create_new, width=32).pack(
        padx=24, pady=(0, 18)
    )
    dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)

    root.wait_window(dialog)
    root.destroy()

    chosen = result.get("path")
    return Path(chosen) if chosen else None


def choose_new_config_file() -> Path | None:
    """Prompts for a config.yaml (existing or new) and, if one was chosen,
    remembers it for future launches. A newly-created file is seeded from
    config.example.yaml. Returns the chosen path, or None if cancelled --
    in which case any previously-remembered choice is left untouched."""
    chosen = _prompt_for_config_file()
    if chosen is None:
        return None

    if not chosen.exists():
        chosen.parent.mkdir(parents=True, exist_ok=True)
        chosen.write_text(EXAMPLE_CONFIG_PATH.read_text())

    POINTER_PATH.write_text(str(chosen))
    return chosen


def resolve_config_path() -> Path:
    """Figures out which config.yaml this run should use. Always prompts
    if nothing has been explicitly chosen before -- never silently reads
    a config.yaml just because one happens to sit next to the app."""
    if POINTER_PATH.exists():
        remembered = Path(POINTER_PATH.read_text().strip())
        if remembered.is_file():
            return remembered
        POINTER_PATH.unlink(missing_ok=True)  # stale pointer -- file moved/deleted

    chosen = choose_new_config_file()
    if chosen is not None:
        return chosen

    # Cancelled entirely -- run this session with blank/example config
    # rather than refuse to start. Nothing is remembered, so next launch
    # prompts again.
    return EXAMPLE_CONFIG_PATH


def main() -> None:
    """Console-script / run.py entry point. Must resolve the config path
    and set VIDEO_UPLOADER_CONFIG_PATH *before* importing video_uploader.web.app,
    since that module loads config at import time."""
    os.environ["VIDEO_UPLOADER_CONFIG_PATH"] = str(resolve_config_path())

    from video_uploader.web.app import main as run_web_app

    run_web_app()
