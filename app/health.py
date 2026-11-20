"""Rule-based health scoring (placeholder for the future AI Reasoning Engine).

Phase 1 only has GitLab-reported runner status + job-failure counts to work
with (no Docker/disk telemetry yet - that needs an on-runner agent, tracked
as a Phase 1.5 follow-up). The scoring rules below are deliberately simple
and easy to extend once richer signals are available.
"""
from dataclasses import dataclass, field


@dataclass
class HealthAssessment:
    score: int
    issues: list[str] = field(default_factory=list)
    recommended_action: str = "No action needed"
    confidence: int = 60


_STATUS_PENALTY = {
    "offline": (50, "Runner offline"),
    "stale": (25, "Runner stale (no recent heartbeat)"),
    "never_contacted": (60, "Runner has never contacted GitLab"),
}

_STATUS_ACTION = {
    "offline": "Check the runner host/VM is powered on and the gitlab-runner service is running",
    "stale": "Verify network connectivity between the runner and GitLab, and check runner logs",
    "never_contacted": "Verify runner registration token/config; runner may not be started yet",
}

_FAILURE_REASON_ACTION = {
    "runner_system_failure": "Investigate runner host health (Docker/disk/system resources)",
    "script_failure": "Review job script/log for an application-level error",
    "stuck_or_timeout_failure": "Check runner capacity/availability; job may have queued too long",
}


def compute_health_score(status: str, failure_count: int, failure_reasons: list[str] | None = None) -> HealthAssessment:
    score = 100
    issues: list[str] = []
    recommended_action = "No action needed"
    confidence = 60

    penalty, message = _STATUS_PENALTY.get(status, (0, ""))
    if penalty:
        score -= penalty
        issues.append(message)
        recommended_action = _STATUS_ACTION.get(status, recommended_action)
        confidence = 90

    if failure_count > 0:
        score -= min(failure_count * 5, 40)
        issues.append(f"{failure_count} failed job(s) in the lookback window")
        if recommended_action == "No action needed":
            top_reason = (failure_reasons or [None])[0]
            recommended_action = _FAILURE_REASON_ACTION.get(
                top_reason, "Review recent failed job logs to identify root cause"
            )
            confidence = 75 if top_reason in _FAILURE_REASON_ACTION else 50

    score = max(0, min(100, score))
    return HealthAssessment(score=score, issues=issues, recommended_action=recommended_action, confidence=confidence)
