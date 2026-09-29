from __future__ import annotations

# Settings the user can change live from the UI, outside of hand-editing
# config.yaml. Persisted to a small sidecar file next to the SQLite DB
# (not written into config.yaml itself, to avoid clobbering its comments/
# formatting on a round-trip). Read by both web/app.py (the settings
# route) and scheduler.py's purge job -- same process, so no IPC needed.

from video_uploader.config import (
    JOB_HISTORY_RETENTION_DAYS_DEFAULT,
    JOB_HISTORY_RETENTION_DAYS_MAX,
    JOB_HISTORY_RETENTION_DAYS_MIN,
    load_config,
)

_RETENTION_OVERRIDE_PATH = load_config().storage.db_path.parent / "job_history_retention_days.txt"


def _clamp(days: int) -> int:
    return max(JOB_HISTORY_RETENTION_DAYS_MIN, min(days, JOB_HISTORY_RETENTION_DAYS_MAX))


def get_retention_days() -> int:
    if _RETENTION_OVERRIDE_PATH.exists():
        try:
            return _clamp(int(_RETENTION_OVERRIDE_PATH.read_text().strip()))
        except ValueError:
            pass  # corrupt sidecar file -- fall through to config.yaml's value
    return load_config().storage.job_history_retention_days


def set_retention_days(days: int) -> int:
    clamped = _clamp(days)
    _RETENTION_OVERRIDE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _RETENTION_OVERRIDE_PATH.write_text(str(clamped))
    return clamped
