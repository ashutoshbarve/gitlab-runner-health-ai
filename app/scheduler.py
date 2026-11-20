"""APScheduler wiring: periodically runs the GitLab collectors."""
import asyncio
import logging
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler

from app.collectors.job_collector import poll_job_failures
from app.collectors.runner_collector import poll_runners
from app.config import Settings
from app.database import SessionLocal

logger = logging.getLogger("runner_health_agent.scheduler")


def _run_collection_cycle(settings: Settings) -> None:
    db = SessionLocal()
    try:
        asyncio.run(_collect(db, settings))
    except Exception:
        logger.exception("Collection cycle failed")
    finally:
        db.close()


async def _collect(db, settings: Settings) -> None:
    runner_count = await poll_runners(db, settings)
    failure_count = await poll_job_failures(db, settings)
    logger.info("Collection cycle complete: %d runners, %d new job failures", runner_count, failure_count)


def start_scheduler(settings: Settings) -> BackgroundScheduler:
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        _run_collection_cycle,
        "interval",
        minutes=settings.poll_interval_minutes,
        args=[settings],
        next_run_time=None,  # first run is triggered explicitly below for immediate feedback
        id="gitlab_collection_cycle",
        max_instances=1,
    )
    scheduler.start()
    # Kick off an immediate first run instead of waiting a full interval.
    scheduler.modify_job("gitlab_collection_cycle", next_run_time=datetime.now())
    return scheduler
