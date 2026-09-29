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

_DATA_DIR = load_config().storage.db_path.parent
_RETENTION_OVERRIDE_PATH = _DATA_DIR / "job_history_retention_days.txt"
_DISMISSED_MISSED_JOBS_PATH = _DATA_DIR / "dismissed_missed_job_ids.txt"


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


def get_dismissed_missed_job_ids() -> set[int]:
    if not _DISMISSED_MISSED_JOBS_PATH.exists():
        return set()
    ids: set[int] = set()
    for line in _DISMISSED_MISSED_JOBS_PATH.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                ids.add(int(line))
            except ValueError:
                pass  # corrupt line -- skip it rather than fail the whole read
    return ids


def dismiss_missed_job_ids(platform_job_ids: list[int]) -> None:
    """Persists dismissed missed-upload banner entries so they stay
    dismissed across relaunches -- previously in-memory only, which meant
    they'd all reappear on every restart."""
    ids = get_dismissed_missed_job_ids()
    ids.update(platform_job_ids)
    _DISMISSED_MISSED_JOBS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _DISMISSED_MISSED_JOBS_PATH.write_text("\n".join(str(i) for i in sorted(ids)))
