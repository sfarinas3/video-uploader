from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlmodel import Session

from video_uploader.core.engine import CoreEngine
from video_uploader.db import get_engine

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 30

_scheduler = AsyncIOScheduler()


def run_startup_sweep() -> None:
    """Mark anything that was due while the app was closed MISSED, once,
    before the poller starts (DESIGN.md §4.4)."""
    with Session(get_engine()) as session:
        affected = CoreEngine(session).sweep_missed_jobs()

    if affected:
        print(
            f"WARNING: {len(affected)} scheduled upload(s) were missed while the app "
            f"was closed: upload job id(s) {affected}. Visit / to choose 'upload now' "
            "or 'reschedule' for each."
        )


def _poll_due_jobs() -> None:
    with Session(get_engine()) as session:
        core_engine = CoreEngine(session)
        due_ids = core_engine.list_due_upload_job_ids()
        if due_ids:
            logger.info("poller found %d due upload job(s): %s", len(due_ids), due_ids)
        for upload_job_id in due_ids:
            core_engine.run_job(upload_job_id)


def start() -> None:
    _scheduler.add_job(
        _poll_due_jobs,
        trigger="interval",
        seconds=POLL_INTERVAL_SECONDS,
        id="poll_due_jobs",
        replace_existing=True,
    )
    _scheduler.start()


def shutdown() -> None:
    _scheduler.shutdown(wait=False)
