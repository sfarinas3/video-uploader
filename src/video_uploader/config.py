from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

if getattr(sys, "frozen", False):
    # Running as a PyInstaller-bundled exe: there's no source tree on disk
    # (the app itself is unpacked into a temp dir at startup), so config.yaml
    # and the data/ folder live next to the exe instead.
    REPO_ROOT = Path(sys.executable).resolve().parent
else:
    REPO_ROOT = Path(__file__).resolve().parent.parent.parent

DEFAULT_CONFIG_PATH = REPO_ROOT / "config.yaml"
EXAMPLE_CONFIG_PATH = REPO_ROOT / "config.example.yaml"


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8000


JOB_HISTORY_RETENTION_DAYS_MIN = 0  # 0 = purge on every cycle, keep no history
JOB_HISTORY_RETENTION_DAYS_MAX = 365
JOB_HISTORY_RETENTION_DAYS_DEFAULT = 30


@dataclass
class StorageConfig:
    db_path: Path = REPO_ROOT / "data" / "video_uploader.sqlite3"
    upload_dir: Path = REPO_ROOT / "data" / "uploads"
    job_history_retention_days: int = JOB_HISTORY_RETENTION_DAYS_DEFAULT


@dataclass
class AppConfig:
    server: ServerConfig
    storage: StorageConfig
    platforms: dict[str, dict[str, str]]
    config_path: Path


def _clamped_retention_days(raw_value: object) -> int:
    try:
        days = int(raw_value)
    except (TypeError, ValueError):
        return JOB_HISTORY_RETENTION_DAYS_DEFAULT
    return max(JOB_HISTORY_RETENTION_DAYS_MIN, min(days, JOB_HISTORY_RETENTION_DAYS_MAX))


def load_config(path: Path | None = None) -> AppConfig:
    """Load config.yaml, falling back to config.example.yaml if the user
    hasn't created their own copy yet (app-level credentials will just be
    blank in that case). If the VIDEO_UPLOADER_CONFIG_PATH env var is set
    (launcher.py sets this when the user picks a config file to reuse), it
    takes priority over the default config.yaml next to the app."""
    override = os.environ.get("VIDEO_UPLOADER_CONFIG_PATH")
    config_path = path or (
        Path(override) if override else
        DEFAULT_CONFIG_PATH if DEFAULT_CONFIG_PATH.exists() else EXAMPLE_CONFIG_PATH
    )
    raw = yaml.safe_load(config_path.read_text()) or {}

    server_raw = raw.get("server", {})
    storage_raw = raw.get("storage", {})

    return AppConfig(
        server=ServerConfig(
            host=server_raw.get("host", "127.0.0.1"),
            port=server_raw.get("port", 8000),
        ),
        storage=StorageConfig(
            db_path=REPO_ROOT / storage_raw.get("db_path", "data/video_uploader.sqlite3"),
            upload_dir=REPO_ROOT / storage_raw.get("upload_dir", "data/uploads"),
            job_history_retention_days=_clamped_retention_days(
                storage_raw.get("job_history_retention_days", JOB_HISTORY_RETENTION_DAYS_DEFAULT)
            ),
        ),
        platforms=raw.get("platforms", {}),
        config_path=config_path,
    )
