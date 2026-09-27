from __future__ import annotations

import json

import keyring

SERVICE_NAME = "video-uploader"


def save_token(platform: str, data: dict) -> None:
    keyring.set_password(SERVICE_NAME, platform, json.dumps(data))


def load_token(platform: str) -> dict | None:
    raw = keyring.get_password(SERVICE_NAME, platform)
    return json.loads(raw) if raw else None


def delete_token(platform: str) -> None:
    try:
        keyring.delete_password(SERVICE_NAME, platform)
    except keyring.errors.PasswordDeleteError:
        pass
