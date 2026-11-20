from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import JobFailure
from app.trend_analyzer import compute_recurrence, top_recurring_jobs


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _add_failure(db, job_id, project_id, job_name, runner_id, days_ago=0):
    db.add(
        JobFailure(
            id=job_id,
            project_id=project_id,
            job_name=job_name,
            runner_id=runner_id,
            recorded_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
        )
    )
    db.commit()


def test_compute_recurrence_counts_same_job_within_window(db_session):
    _add_failure(db_session, 1, "100", "build:test", runner_id=5, days_ago=1)
    _add_failure(db_session, 2, "100", "build:test", runner_id=5, days_ago=2)
    _add_failure(db_session, 3, "100", "other:job", runner_id=5, days_ago=1)

    recurrence = compute_recurrence(db_session, project_id="100", job_name="build:test", runner_id=5, window_days=7)

    assert recurrence.same_job_failure_count == 2
    assert recurrence.same_runner_failure_count == 3  # all three ran on runner 5


def test_compute_recurrence_excludes_entries_outside_window(db_session):
    _add_failure(db_session, 1, "100", "build:test", runner_id=5, days_ago=10)

    recurrence = compute_recurrence(db_session, project_id="100", job_name="build:test", runner_id=5, window_days=7)

    assert recurrence.same_job_failure_count == 0


def test_recurrence_is_recurring_threshold():
    from app.trend_analyzer import RecurrenceInfo

    assert RecurrenceInfo(same_job_failure_count=3, same_runner_failure_count=3, window_days=7).is_recurring
    assert not RecurrenceInfo(same_job_failure_count=2, same_runner_failure_count=2, window_days=7).is_recurring


def test_top_recurring_jobs_orders_by_failure_count_and_respects_min_count(db_session):
    _add_failure(db_session, 1, "100", "flaky:test", runner_id=1, days_ago=1)
    _add_failure(db_session, 2, "100", "flaky:test", runner_id=1, days_ago=2)
    _add_failure(db_session, 3, "100", "flaky:test", runner_id=1, days_ago=3)
    _add_failure(db_session, 4, "200", "rare:job", runner_id=2, days_ago=1)

    results = top_recurring_jobs(db_session, window_days=7, min_count=2)

    assert len(results) == 1
    assert results[0]["job_name"] == "flaky:test"
    assert results[0]["failure_count"] == 3
