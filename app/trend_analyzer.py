"""Basic trend/recurrence analysis over previously recorded job failures.

This is deliberately simple SQL aggregation (no AI involved) - it gives the
AI reasoning engine useful context (e.g. "this exact job has failed 6 times
this week" strongly suggests a flaky test rather than infrastructure rot)
and powers the /trends API for human consumption.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import JobFailure


@dataclass
class RecurrenceInfo:
    same_job_failure_count: int  # same project+job name, within the window
    same_runner_failure_count: int  # any job on the same runner, within the window
    window_days: int

    @property
    def is_recurring(self) -> bool:
        return self.same_job_failure_count >= 3

    def as_prompt_context(self) -> str:
        parts = [f"This exact job has failed {self.same_job_failure_count} time(s) in the last {self.window_days} days."]
        if self.same_runner_failure_count > self.same_job_failure_count:
            parts.append(
                f"The runner that executed it has had {self.same_runner_failure_count} failed job(s) total "
                f"(across different jobs) in the same window, which may indicate a runner-wide issue rather than "
                f"a problem with this specific job."
            )
        return " ".join(parts)


def compute_recurrence(
    db: Session, project_id: str, job_name: str, runner_id: int | None, window_days: int
) -> RecurrenceInfo:
    since = datetime.now(timezone.utc) - timedelta(days=window_days)

    same_job_count = db.execute(
        select(func.count())
        .select_from(JobFailure)
        .where(JobFailure.project_id == str(project_id))
        .where(JobFailure.job_name == job_name)
        .where(JobFailure.recorded_at >= since)
    ).scalar_one()

    same_runner_count = 0
    if runner_id is not None:
        same_runner_count = db.execute(
            select(func.count())
            .select_from(JobFailure)
            .where(JobFailure.runner_id == runner_id)
            .where(JobFailure.recorded_at >= since)
        ).scalar_one()

    return RecurrenceInfo(
        same_job_failure_count=same_job_count,
        same_runner_failure_count=same_runner_count,
        window_days=window_days,
    )


def top_recurring_jobs(db: Session, window_days: int, min_count: int = 2, limit: int = 20) -> list[dict]:
    """Aggregate view for the /trends API: which (project, job) pairs fail most often."""
    since = datetime.now(timezone.utc) - timedelta(days=window_days)
    rows = db.execute(
        select(
            JobFailure.project_id,
            JobFailure.job_name,
            func.count().label("failure_count"),
            func.max(JobFailure.recorded_at).label("last_failed_at"),
        )
        .where(JobFailure.recorded_at >= since)
        .group_by(JobFailure.project_id, JobFailure.job_name)
        .having(func.count() >= min_count)
        .order_by(func.count().desc())
        .limit(limit)
    ).all()

    return [
        {
            "project_id": r.project_id,
            "job_name": r.job_name,
            "failure_count": r.failure_count,
            "last_failed_at": r.last_failed_at,
        }
        for r in rows
    ]
