"""Collects runner availability from GitLab, upserts into the DB, computes a
health score per runner, and alerts on status changes / score drops below
the configured threshold.
"""
import json
import logging
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.gitlab_client import GitLabClient
from app.health import compute_health_score
from app.models import JobFailure, Runner, RunnerStatusEvent
from app.notifier import TeamsNotifier

logger = logging.getLogger("runner_health_agent.runner_collector")


def _normalize_status(raw: dict) -> str:
    status = raw.get("status")
    if status:
        return status
    return "online" if raw.get("online") else "offline"


def _failure_count_and_reasons(db: Session, runner_id: int, since: datetime) -> tuple[int, list[str]]:
    rows = db.execute(
        select(JobFailure.failure_reason)
        .where(JobFailure.runner_id == runner_id)
        .where(func.coalesce(JobFailure.finished_at, JobFailure.created_at) >= since)
    ).all()
    reasons = [r[0] for r in rows if r[0]]
    return len(rows), reasons


async def poll_runners(db: Session, settings: Settings, client_factory=httpx.AsyncClient) -> int:
    """Fetch current runner status from GitLab, update DB + health scores, alert on changes.

    Returns the number of runners processed.
    """
    gitlab = GitLabClient(settings)
    notifier = TeamsNotifier(settings)
    since = datetime.now(timezone.utc) - timedelta(hours=settings.failure_lookback_hours)

    raw_runners: list[dict] = []
    async with client_factory() as http_client:
        if settings.gitlab_runner_scope == "instance":
            try:
                raw_runners = await gitlab.list_instance_runners(http_client)
            except httpx.HTTPError as exc:
                logger.error("Failed to fetch instance runners: %s", exc)

        seen_ids = {r["id"] for r in raw_runners}

        if settings.gitlab_runner_scope == "group":
            for group_id in settings.group_id_list:
                try:
                    group_runners = await gitlab.list_group_runners(http_client, group_id)
                except httpx.HTTPError as exc:
                    logger.error("Failed to fetch runners for group %s: %s", group_id, exc)
                    continue
                for r in group_runners:
                    if r["id"] not in seen_ids:
                        raw_runners.append(r)
                        seen_ids.add(r["id"])

        for project_id in settings.project_id_list:
            try:
                project_runners = await gitlab.list_project_runners(http_client, project_id)
            except httpx.HTTPError as exc:
                logger.error("Failed to fetch runners for project %s: %s", project_id, exc)
                continue
            for r in project_runners:
                if r["id"] not in seen_ids:
                    raw_runners.append(r)
                    seen_ids.add(r["id"])

    for raw in raw_runners:
        runner_id = raw["id"]
        new_status = _normalize_status(raw)
        contacted_at = raw.get("contacted_at")
        contacted_dt = datetime.fromisoformat(contacted_at.replace("Z", "+00:00")) if contacted_at else None

        failure_count, failure_reasons = _failure_count_and_reasons(db, runner_id, since)
        assessment = compute_health_score(new_status, failure_count, failure_reasons)

        runner = db.get(Runner, runner_id)
        previous_status = runner.status if runner else None

        if runner is None:
            runner = Runner(id=runner_id)
            db.add(runner)

        runner.description = raw.get("description", "") or ""
        runner.runner_type = raw.get("runner_type", "") or ""
        runner.status = new_status
        runner.is_paused = not raw.get("active", True)
        runner.tag_list = ",".join(raw.get("tag_list") or [])
        runner.ip_address = raw.get("ip_address") or ""
        runner.contacted_at = contacted_dt
        runner.health_score = assessment.score
        runner.issues = json.dumps(assessment.issues)
        db.commit()

        status_changed = previous_status is not None and previous_status != new_status
        if status_changed:
            db.add(
                RunnerStatusEvent(
                    runner_id=runner_id,
                    previous_status=previous_status,
                    new_status=new_status,
                )
            )
            db.commit()
            if new_status in ("offline", "stale", "never_contacted"):
                await notifier.send_runner_status_alert(
                    db, raw, assessment.score, assessment.issues, assessment.recommended_action, assessment.confidence
                )
            elif new_status == "online" and previous_status in ("offline", "stale", "never_contacted"):
                await notifier.send_runner_status_alert(
                    db, raw, assessment.score, ["Runner recovered"], "No action needed", 90
                )

        below_threshold = assessment.score < settings.health_score_alert_threshold
        if below_threshold and not runner.score_alert_active and not status_changed:
            await notifier.send_runner_status_alert(
                db, raw, assessment.score, assessment.issues, assessment.recommended_action, assessment.confidence
            )
        runner.score_alert_active = below_threshold
        db.commit()

    return len(raw_runners)
