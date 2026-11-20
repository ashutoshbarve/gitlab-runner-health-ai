"""Teams notifications via a Power Automate "Post card in a chat or channel"
workflow webhook.

That webhook trigger ("When a Teams webhook request is received") expects
the POST body to itself be a card payload - an Adaptive Card wrapped in the
`type: message` / `attachments` envelope (or a legacy MessageCard) - not an
arbitrary flat JSON object to be mapped later in the flow designer. See:
https://learn.microsoft.com/connectors/teams/#microsoft-teams-webhook
"""
import json
import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import AlertLog

logger = logging.getLogger("runner_health_agent.notifier")


def _fact(title: str, value: Any) -> dict:
    return {"title": title, "value": "" if value is None else str(value)}


def _adaptive_card(title: str, facts: list[dict], color: str = "attention", open_url: str | None = None) -> dict:
    body: list[dict] = [
        {
            "type": "TextBlock",
            "text": title,
            "weight": "Bolder",
            "size": "Medium",
            "wrap": True,
            "color": color,
        },
        {"type": "FactSet", "facts": facts},
    ]
    card: dict[str, Any] = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": body,
        # Renders the card at full channel width in Teams instead of the
        # default narrow column - see Microsoft's "Full width Adaptive Card"
        # docs (msteams.width = "Full").
        "msteams": {"width": "Full"},
    }
    if open_url:
        card["actions"] = [{"type": "Action.OpenUrl", "title": "View in GitLab", "url": open_url}]

    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": card,
            }
        ],
    }


class TeamsNotifier:
    def __init__(self, settings: Settings):
        self._webhook_url = settings.teams_webhook_url

    async def _send(self, db: Session, alert_type: str, subject: str, card_payload: dict) -> None:
        success, error = False, ""

        if not self._webhook_url:
            logger.warning("TEAMS_WEBHOOK_URL not configured; alert logged only: %s", card_payload)
            error = "TEAMS_WEBHOOK_URL not configured"
        else:
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    resp = await client.post(self._webhook_url, json=card_payload)
                    resp.raise_for_status()
                success = True
            except httpx.HTTPError as exc:
                error = str(exc)
                logger.error("Failed to send Teams alert: %s", exc)

        db.add(
            AlertLog(
                alert_type=alert_type,
                subject=subject,
                payload=json.dumps(card_payload),
                success=success,
                error=error,
            )
        )
        db.commit()

    async def send_runner_status_alert(
        self, db: Session, runner: dict, health_score: int, issues: list[str], recommended_action: str, confidence: int
    ) -> None:
        status = runner.get("status")
        color = "good" if status == "online" else "attention"
        facts = [
            _fact("Runner", f"{runner.get('description') or 'runner-' + str(runner['id'])} (#{runner['id']})"),
            _fact("Status", status),
            _fact("Health score", f"{health_score}/100"),
            _fact("Issues", "; ".join(issues) or "None"),
            _fact("Recommended action", recommended_action),
            _fact("Confidence", f"{confidence}%"),
            _fact("Timestamp", datetime.now(timezone.utc).isoformat()),
        ]
        card = _adaptive_card("🚨 Runner Health Alert", facts, color=color)
        await self._send(db, "runner_status", f"runner:{runner['id']}", card)

    async def send_job_failure_alert(self, db: Session, job: dict, runner_id: int | None, diagnosis=None) -> None:
        facts = [
            _fact("Project", job.get("project_id")),
            _fact("Job", job.get("name")),
            _fact("Stage", job.get("stage")),
            _fact("Ref", job.get("ref")),
            _fact("GitLab failure_reason", job.get("failure_reason")),
            _fact("Runner", runner_id if runner_id is not None else "unknown"),
        ]
        if job.get("dependency_analysis"):
            facts.append(_fact("Missing dependency", job["dependency_analysis"]))
        if diagnosis is not None:
            facts.extend(
                [
                    _fact("AI root cause", diagnosis.root_cause),
                    _fact("AI category", diagnosis.category),
                    _fact("AI confidence", f"{diagnosis.confidence}%"),
                    _fact("Recommended action", diagnosis.recommended_action),
                ]
            )
            if diagnosis.tool_calls_made:
                facts.append(_fact("AI investigation steps", ", ".join(diagnosis.tool_calls_made)))
        facts.append(_fact("Timestamp", datetime.now(timezone.utc).isoformat()))
        card = _adaptive_card("❌ Build Failure Detected", facts, color="attention", open_url=job.get("web_url"))
        await self._send(db, "job_failure", f"job:{job.get('id')}", card)
