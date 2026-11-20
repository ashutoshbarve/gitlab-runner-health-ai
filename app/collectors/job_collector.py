"""Collects recently failed GitLab CI jobs, correlates them to the runner
that executed them, enriches each new failure with an AI root-cause
diagnosis (Phase 2), and stores/alerts on newly seen failures.
"""
import json
import logging
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import ai_reasoning
from app.config import Settings
from app.dependency_analyzer import analyze_missing_dependency
from app.gitlab_client import GitLabClient
from app.models import JobFailure
from app.notifier import TeamsNotifier
from app.trend_analyzer import compute_recurrence

logger = logging.getLogger("runner_health_agent.job_collector")


async def _resolve_project_ids(
    http_client: httpx.AsyncClient, gitlab: GitLabClient, settings: Settings
) -> list[str]:
    """Explicit GITLAB_PROJECT_IDS take priority. Otherwise, if group scope is
    configured, auto-discover every project under those groups (including
    subgroups) so job failures are tracked fleet-wide without listing each
    project by hand.
    """
    if settings.project_id_list:
        return settings.project_id_list

    if settings.gitlab_runner_scope == "group" and settings.group_id_list:
        project_ids: list[str] = []
        for group_id in settings.group_id_list:
            try:
                projects = await gitlab.list_group_projects(http_client, group_id)
            except httpx.HTTPError as exc:
                logger.error("Failed to list projects for group %s: %s", group_id, exc)
                continue
            project_ids.extend(str(p["id"]) for p in projects)
        return project_ids

    return []


async def poll_job_failures(db: Session, settings: Settings, client_factory=httpx.AsyncClient) -> int:
    """Fetch recently failed jobs for all configured/discovered projects. Returns count of newly recorded failures."""
    gitlab = GitLabClient(settings)
    notifier = TeamsNotifier(settings)
    since = datetime.now(timezone.utc) - timedelta(hours=settings.failure_lookback_hours)
    new_count = 0

    async with client_factory() as http_client:
        project_ids = await _resolve_project_ids(http_client, gitlab, settings)

        for project_id in project_ids:
            try:
                jobs = await gitlab.list_failed_jobs(http_client, project_id, since=since)
            except httpx.HTTPError as exc:
                logger.error("Failed to fetch failed jobs for project %s: %s", project_id, exc)
                continue

            for job in jobs:
                job_id = job.get("id")
                if job_id is None:
                    continue
                exists = db.execute(select(JobFailure).where(JobFailure.id == job_id)).scalar_one_or_none()
                if exists:
                    continue

                runner_info = job.get("runner") or {}
                runner_id = runner_info.get("id")

                def _parse(ts: str | None):
                    return datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None

                failure = JobFailure(
                    id=job_id,
                    project_id=str(project_id),
                    runner_id=runner_id,
                    job_name=job.get("name", ""),
                    stage=job.get("stage", ""),
                    ref=job.get("ref", ""),
                    failure_reason=job.get("failure_reason", ""),
                    web_url=job.get("web_url", ""),
                    created_at=_parse(job.get("created_at")),
                    finished_at=_parse(job.get("finished_at")),
                )
                db.add(failure)

                diagnosis = await _diagnose_and_store(db, settings, gitlab, http_client, failure)
                db.commit()
                new_count += 1

                job_with_project = {
                    **job, "project_id": project_id, "dependency_analysis": failure.dependency_analysis,
                }
                await notifier.send_job_failure_alert(db, job_with_project, runner_id, diagnosis)
                failure.notified = True
                db.commit()

    return new_count


async def _diagnose_and_store(
    db: Session, settings: Settings, gitlab: GitLabClient, http_client: httpx.AsyncClient, failure: JobFailure
) -> ai_reasoning.AIDiagnosis:
    """Fetch the job's log, compute recurrence, run AI diagnosis, and store
    the results onto `failure` (caller is responsible for db.commit()).
    Shared by the live collector and on-demand re-diagnosis, so a bug fix in
    the AI pipeline benefits both new and previously-recorded failures.
    """
    try:
        log_text = await gitlab.get_job_trace(http_client, failure.project_id, failure.id)
    except httpx.HTTPError as exc:
        logger.warning("Could not fetch log trace for job %s: %s", failure.id, exc)
        log_text = failure.log_excerpt  # fall back to whatever was already stored, if any

    if failure.failure_reason == "missing_dependency_failure":
        try:
            failure.dependency_analysis = await analyze_missing_dependency(
                gitlab, http_client, failure.project_id, failure.id
            ) or failure.dependency_analysis
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            logger.warning("Could not analyze missing dependency for job %s: %s", failure.id, exc)

    recurrence = compute_recurrence(
        db, project_id=failure.project_id, job_name=failure.job_name, runner_id=failure.runner_id,
        window_days=settings.trend_window_days,
    )
    failure.recurrence_count = recurrence.same_job_failure_count
    failure.log_excerpt = log_text[-settings.log_excerpt_max_chars :]

    diagnosis = await ai_reasoning.diagnose_agentic(
        settings,
        db=db,
        job_metadata={
            "job_name": failure.job_name,
            "stage": failure.stage,
            "ref": failure.ref,
            "failure_reason": failure.failure_reason,
            "dependency_analysis": failure.dependency_analysis,
        },
        full_log=log_text,
        recurrence=recurrence,
        runner_id=failure.runner_id,
        project_id=failure.project_id,
    )
    if not diagnosis.succeeded and failure.dependency_analysis:
        # The dependency analysis is verified from the GitLab API, so it's a better answer than a blank AI fallback.
        diagnosis.root_cause = failure.dependency_analysis
        diagnosis.category = "Missing Dependency"
        diagnosis.confidence = 80
        diagnosis.recommended_action = "Re-run the full pipeline (or the upstream job first) to regenerate the artifacts."
    failure.ai_root_cause = diagnosis.root_cause
    failure.ai_category = diagnosis.category
    failure.ai_confidence = diagnosis.confidence
    failure.ai_recommended_action = diagnosis.recommended_action
    failure.ai_error = diagnosis.error
    failure.ai_tool_calls = json.dumps(diagnosis.tool_calls_made)
    return diagnosis


async def rediagnose_failure(
    db: Session, settings: Settings, failure: JobFailure, client_factory=httpx.AsyncClient
) -> ai_reasoning.AIDiagnosis:
    """Re-run AI diagnosis for an already-recorded failure - e.g. after a bug
    fix in the AI reasoning pipeline (see app/api/failures.py for the
    endpoints that call this). Re-fetches the job's full log from GitLab
    fresh rather than reusing the possibly-truncated stored excerpt, so
    tool-calling (get_more_log_context) has full context available again.
    """
    gitlab = GitLabClient(settings)
    async with client_factory() as http_client:
        diagnosis = await _diagnose_and_store(db, settings, gitlab, http_client, failure)
    db.commit()
    return diagnosis
