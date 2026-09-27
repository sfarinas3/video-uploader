from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config.yaml"
EXAMPLE_CONFIG_PATH = REPO_ROOT / "config.example.yaml"


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8000


@dataclass
class StorageConfig:
    db_path: Path = REPO_ROOT / "data" / "video_uploader.sqlite3"
    upload_dir: Path = REPO_ROOT / "data" / "uploads"


@dataclass
class AppConfig:
    server: ServerConfig
    storage: StorageConfig
    platforms: dict[str, dict[str, str]]


def load_config(path: Path | None = None) -> AppConfig:
    """Load config.yaml, falling back to config.example.yaml if the user
    hasn't created their own copy yet (app-level credentials will just be
    blank in that case)."""
    config_path = path or (
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
        ),
        platforms=raw.get("platforms", {}),
    )
