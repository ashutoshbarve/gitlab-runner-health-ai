"""Explains GitLab `missing_dependency_failure` job failures.

Such a job never starts (so its log is empty): GitLab could not hand it the
artifacts of the upstream job(s) it depends on, via `needs:` (with
artifacts) or `dependencies:`. The usual causes are that the upstream
artifacts expired before the job was (re)run, the upstream job failed or was
skipped, or it never produced an artifacts archive. This module works out
which upstream jobs are involved and why, from the GitLab API alone, so the
diagnosis and alert can name them instead of saying "a dependency is missing".
"""
import logging
from datetime import datetime, timezone

import httpx
import yaml

from app.gitlab_client import GitLabClient

logger = logging.getLogger("runner_health_agent.dependency_analyzer")

DEFAULT_STAGES = [".pre", "build", "test", "deploy", ".post"]
_RETRY_GAP_SECONDS = 3600


def _parse_ts(ts: str | None) -> datetime | None:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None


def _date(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d") if dt else "unknown date"


def _humanize_gap(seconds: float) -> str:
    days = int(seconds // 86400)
    if days >= 1:
        return f"{days} day{'s' if days != 1 else ''}"
    hours = int(seconds // 3600)
    return f"{hours} hour{'s' if hours != 1 else ''}"


def upstream_job_names(ci_yaml: str, job_name: str, stage: str) -> list[str] | None:
    """Names of the jobs whose artifacts `job_name` consumes, or None if the
    CI config can't tell us (unparseable, or the job isn't defined in it)."""
    try:
        config = yaml.safe_load(ci_yaml)
    except yaml.YAMLError:
        return None
    if not isinstance(config, dict) or not isinstance(config.get(job_name), dict):
        return None
    job_config = config[job_name]

    needs = job_config.get("needs")
    if needs is not None:
        names = []
        for need in needs if isinstance(needs, list) else [needs]:
            if isinstance(need, str):
                names.append(need)
            elif isinstance(need, dict) and need.get("job") and need.get("artifacts", True):
                names.append(need["job"])
        return names

    dependencies = job_config.get("dependencies")
    if dependencies is not None:
        return [str(d) for d in dependencies]

    # Neither key set: GitLab passes artifacts from every job in earlier stages.
    stages = config.get("stages") or DEFAULT_STAGES
    if stage not in stages:
        return None
    earlier = set(stages[: stages.index(stage)])
    return [
        name
        for name, cfg in config.items()
        if isinstance(cfg, dict)
        and not str(name).startswith(".")
        and ("script" in cfg or "trigger" in cfg)
        and cfg.get("stage", "test") in earlier
    ]


def _latest_by_name(pipeline_jobs: list[dict]) -> dict[str, dict]:
    latest: dict[str, dict] = {}
    for job in pipeline_jobs:
        name = job.get("name")
        if name and (name not in latest or job["id"] > latest[name]["id"]):
            latest[name] = job
    return latest


def _artifacts_expired(upstream: dict, failed_at: datetime) -> bool:
    has_archive = any(a.get("file_type") == "archive" for a in upstream.get("artifacts") or [])
    expires_at = _parse_ts(upstream.get("artifacts_expire_at"))
    return not has_archive and expires_at is not None and expires_at <= failed_at


def _describe_upstream(name: str, upstream: dict | None, failed_at: datetime) -> tuple[str, bool]:
    """One-sentence finding for an upstream job, and whether it explains the failure."""
    if upstream is None:
        return f"'{name}' is not part of this pipeline (excluded by rules or never created).", True

    label = f"'{name}' (job #{upstream['id']})"
    status = upstream.get("status")
    if status != "success":
        return f"{label} has status '{status}', so it produced no usable artifacts.", True

    if _artifacts_expired(upstream, failed_at):
        expires_at = _parse_ts(upstream.get("artifacts_expire_at"))
        gap = _humanize_gap((failed_at - expires_at).total_seconds())
        return (
            f"{label} succeeded on {_date(_parse_ts(upstream.get('finished_at')))} but its artifacts "
            f"expired on {_date(expires_at)} ({gap} before this job ran).",
            True,
        )

    if any(a.get("file_type") == "archive" for a in upstream.get("artifacts") or []):
        return f"{label} succeeded and its artifacts are present.", False
    return f"{label} succeeded but produced no artifacts archive (check its artifacts:paths).", True


async def analyze_missing_dependency(
    gitlab: GitLabClient, http_client: httpx.AsyncClient, project_id: str, job_id: int
) -> str:
    """Single-paragraph explanation of which upstream job(s) a
    missing_dependency_failure job depends on and why they are unavailable.
    Returns "" if it can't be determined."""
    job = await gitlab.get_job(http_client, project_id, job_id)
    pipeline_id = (job.get("pipeline") or {}).get("id")
    if not pipeline_id:
        return ""

    pipeline = await gitlab.get_pipeline(http_client, project_id, pipeline_id)
    pipeline_jobs = await gitlab.list_pipeline_jobs(http_client, project_id, pipeline_id)
    try:
        ci_yaml = await gitlab.get_merged_ci_config(http_client, project_id, pipeline["sha"])
    except (httpx.HTTPError, KeyError) as exc:
        logger.warning("Could not fetch merged CI config for project %s: %s", project_id, exc)
        ci_yaml = ""

    job_name, stage = job.get("name", ""), job.get("stage", "")
    latest = _latest_by_name(pipeline_jobs)
    failed_at = _parse_ts(job.get("finished_at")) or datetime.now(timezone.utc)

    names = upstream_job_names(ci_yaml, job_name, stage) if ci_yaml else None
    if names is not None:
        findings = [_describe_upstream(name, latest.get(name), failed_at) for name in names]
        intro = (
            f"Job '{job_name}' (stage '{stage}') never started because the artifacts it needs from "
            f"{', '.join(repr(n) for n in names) or 'upstream jobs'} were unavailable."
        )
    else:
        # CI config unavailable: report any other job in the pipeline that can't supply artifacts.
        findings = [
            _describe_upstream(name, upstream, failed_at)
            for name, upstream in latest.items()
            if name != job_name and (upstream.get("status") != "success" or _artifacts_expired(upstream, failed_at))
        ]
        intro = (
            f"Job '{job_name}' (stage '{stage}') never started because an upstream job's artifacts "
            f"were unavailable (its exact needs/dependencies could not be read from the CI config)."
        )

    explaining = [text for text, explains in findings if explains]
    if names is None and not explaining:
        return ""
    if not explaining:
        return f"{intro} " + " ".join(text for text, _ in findings)

    parts = [intro, *explaining]
    pipeline_created = _parse_ts(pipeline.get("created_at"))
    job_created = _parse_ts(job.get("created_at"))
    if pipeline_created and job_created and (job_created - pipeline_created).total_seconds() > _RETRY_GAP_SECONDS:
        gap = _humanize_gap((job_created - pipeline_created).total_seconds())
        parts.append(
            f"The pipeline was created on {_date(pipeline_created)} but this job was created on "
            f"{_date(job_created)} ({gap} later), so it was most likely retried after the upstream artifacts expired."
        )
    parts.append("Re-run the full pipeline (or the upstream job first) to regenerate them.")
    return " ".join(parts)
