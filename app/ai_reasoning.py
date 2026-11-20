"""AI Reasoning Engine (Phase 2): LLM-backed root-cause diagnosis for failed
CI jobs, replacing the fixed rule lookup with actual reasoning over the job's
log output, metadata, and recurrence history.

Supports two backends (AI_PROVIDER):
  - "openai_compatible": any OpenAI-style /v1/chat/completions endpoint
    (default - e.g. an internal model broker such as the one used for this
    project's own CI code-review automation).
  - "ollama": a local/self-hosted Ollama instance, for offline dev use.

`diagnose()` is a single-shot call: fixed context in, one JSON answer out.
`diagnose_agentic()` instead gives the model tools (fetch more log context,
check the runner's recent failure history, check past diagnoses for this
exact job) and lets it decide whether to call them before answering - a
genuine reason/act/observe loop rather than a classifier. It only engages
for ai_provider=openai_compatible (where tool calling is confirmed
supported); other configurations fall back to the single-shot path.

Both are designed to fail soft - if the model is unreachable, slow, or
returns malformed output, callers get a usable fallback AIDiagnosis instead
of an exception, so a flaky/unavailable LLM never breaks the pipeline.
"""
import json
import logging
import re
from dataclasses import dataclass, field

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.trend_analyzer import RecurrenceInfo

logger = logging.getLogger("runner_health_agent.ai_reasoning")

CATEGORIES = [
    "Disk Full",
    "Docker Failure",
    "Vault Connectivity",
    "Permission Issues",
    "Network Connectivity",
    "Missing Dependency",
    "Script/Test Failure",
    "Flaky/Intermittent",
    "Other",
]

_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
# Models sometimes echo raw file paths (e.g. Windows CI paths like
# C:\GitLab-Runner\builds\...) into their JSON response without escaping the
# backslashes, which breaks strict JSON parsing ("Invalid \escape"). Letters
# like b/n/r/t/f/u also happen to be valid JSON escapes (backspace, newline,
# etc.), so a naive "only repair clearly-invalid escapes" approach silently
# corrupts path segments like "\builds" or "\nbp9..." into control
# characters instead of raising an error. Since these fields are short,
# single-sentence free text that should never legitimately contain a raw
# control character, only `\"` and `\\` are treated as intentional escapes -
# everything else gets repaired to a literal backslash.
_INVALID_BACKSLASH_RE = re.compile(r'\\(?!["\\])')


@dataclass
class AIDiagnosis:
    root_cause: str
    category: str
    confidence: int
    recommended_action: str
    error: str = ""
    tool_calls_made: list[str] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return not self.error


def _describe_error(exc: Exception) -> str:
    # httpx timeouts stringify to "" (or a bare "timed out"), so always lead with the type.
    message = str(exc).strip()
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def _fallback(reason: str) -> AIDiagnosis:
    return AIDiagnosis(
        root_cause=f"AI analysis unavailable ({reason}). Falling back to the recorded GitLab failure_reason.",
        category="Other",
        confidence=0,
        recommended_action="Review the job log manually.",
        error=reason,
    )


def _parse_diagnosis(raw_text: str) -> AIDiagnosis:
    # Chat-completion models (unlike Ollama's forced JSON mode) sometimes
    # wrap the JSON in a ```json ... ``` fence despite instructions not to -
    # strip that defensively before parsing.
    cleaned = _CODE_FENCE_RE.sub("", raw_text or "").strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        # Retry once with raw backslashes repaired (see _INVALID_BACKSLASH_RE)
        # before giving up - this recovers the common case of an otherwise
        # well-formed response containing an unescaped file path.
        repaired = _INVALID_BACKSLASH_RE.sub(r"\\\\", cleaned)
        parsed = json.loads(repaired)
    category = parsed.get("category") if parsed.get("category") in CATEGORIES else "Other"
    confidence = int(parsed.get("confidence", 0))
    confidence = max(0, min(100, confidence))
    return AIDiagnosis(
        root_cause=str(parsed.get("root_cause", "")).strip() or "No root cause provided by the model.",
        category=category,
        confidence=confidence,
        recommended_action=str(parsed.get("recommended_action", "")).strip() or "Review the job log manually.",
    )


def build_prompt(
    job_metadata: dict, log_excerpt: str, recurrence: RecurrenceInfo, tools_available: bool = False
) -> str:
    categories = ", ".join(CATEGORIES)
    log_section = log_excerpt.strip() or "(no log output available)"
    tools_note = (
        "\nYou have tools available to fetch more log context, the runner's recent failure history, or past "
        "diagnoses for this exact job. If your confidence would be below 70 based on the information given "
        "alone, you MUST call at least one relevant tool before answering rather than guessing or recommending "
        "that someone else look deeper - investigate it yourself first.\n"
        if tools_available
        else ""
    )
    dependency_section = (
        "\nPipeline dependency analysis (verified from the GitLab API - treat as authoritative; the job never "
        "started, so its log is empty). Your root_cause MUST name the specific upstream job(s) and why their "
        "artifacts are unavailable, use category \"Missing Dependency\", and need not call any tools:\n"
        f"{job_metadata['dependency_analysis']}\n"
        if job_metadata.get("dependency_analysis")
        else ""
    )
    return f"""You are an SRE diagnosing a failed CI/CD job. Analyze the information below and
respond with ONLY a single JSON object (no markdown, no code fences, no commentary) matching exactly this schema:
{{"root_cause": "<one or two sentence explanation>", "category": "<one of: {categories}>", "confidence": <integer 0-100>, "recommended_action": "<concise actionable next step>"}}
{tools_note}
Job name: {job_metadata.get('job_name')}
Stage: {job_metadata.get('stage')}
Ref: {job_metadata.get('ref')}
GitLab-reported failure_reason: {job_metadata.get('failure_reason')}
Recurrence context: {recurrence.as_prompt_context()}
{dependency_section}
Log excerpt (most recent output, may be truncated):
---
{log_section}
---
"""


async def _call_openai_compatible(client: httpx.AsyncClient, settings: Settings, prompt: str, model: str) -> str:
    headers = {"authorization": f"Bearer {settings.ai_api_key}", "content-type": "application/json"}
    resp = await client.post(
        settings.ai_api_url,
        headers=headers,
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
        },
        timeout=settings.ai_request_timeout_seconds,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


async def _call_ollama(client: httpx.AsyncClient, settings: Settings, prompt: str) -> str:
    resp = await client.post(
        f"{settings.ollama_url.rstrip('/')}/api/generate",
        json={
            "model": settings.ollama_model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.1},
        },
        timeout=settings.ai_request_timeout_seconds,
    )
    resp.raise_for_status()
    return resp.json()["response"]


def _default_client_factory():
    # Connect directly to the configured model endpoint, without inheriting
    # proxy settings from the process environment.
    return httpx.AsyncClient(trust_env=False)


async def diagnose(
    settings: Settings,
    job_metadata: dict,
    log_excerpt: str,
    recurrence: RecurrenceInfo,
    client_factory=None,
) -> AIDiagnosis:
    """Single-shot diagnosis: fixed context in, one JSON answer out."""
    if not settings.ai_reasoning_enabled:
        return _fallback("AI reasoning disabled (AI_REASONING_ENABLED=false)")

    prompt = build_prompt(job_metadata, log_excerpt, recurrence)
    factory = client_factory or _default_client_factory

    try:
        async with factory() as client:
            if settings.ai_provider == "ollama":
                raw_text = await _call_ollama(client, settings, prompt)
            else:
                raw_text = None
                failures: list[str] = []
                for model in settings.ai_model_candidates:
                    try:
                        raw_text = await _call_openai_compatible(client, settings, prompt, model)
                        break
                    except httpx.HTTPError as exc:
                        logger.error("AI reasoning request to model %s failed: %s", model, _describe_error(exc))
                        failures.append(f"{model}: {_describe_error(exc)}")
                if raw_text is None:
                    return _fallback(f"AI request failed: {'; '.join(failures)}")
    except httpx.HTTPError as exc:
        logger.error("AI reasoning request failed: %s", _describe_error(exc))
        return _fallback(f"AI request failed: {_describe_error(exc)}")

    try:
        return _parse_diagnosis(raw_text)
    except (KeyError, ValueError, json.JSONDecodeError, TypeError) as exc:
        logger.error("Could not parse AI model response as the expected JSON schema: %s", exc)
        return _fallback(f"Could not parse model output: {exc}")


# ---------------------------------------------------------------------------
# Agentic tool-calling path
# ---------------------------------------------------------------------------

TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "get_more_log_context",
            "description": (
                "Fetch additional lines from earlier in the job's full log, before the excerpt already "
                "provided. Use this if the provided excerpt seems to be missing the actual root cause."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "max_chars": {
                        "type": "integer",
                        "description": "How many additional characters of earlier log content to retrieve.",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_runner_failure_history",
            "description": (
                "List recent failed jobs (job name, failure reason, when) that ran on the same GitLab runner "
                "that executed this job, to help determine if the issue is runner-specific rather than "
                "job-specific."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_job_diagnosis_history",
            "description": (
                "List past AI diagnoses (category, root cause) previously recorded for this exact job name in "
                "this project, to check if this is a previously-seen recurring issue."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]

_DEFAULT_EXCERPT_CHARS = 4000


@dataclass
class ToolContext:
    db: Session
    full_log: str
    runner_id: int | None
    project_id: str
    job_name: str
    already_shown_chars: int = _DEFAULT_EXCERPT_CHARS


def _tool_get_more_log_context(ctx: ToolContext, max_chars: int = _DEFAULT_EXCERPT_CHARS) -> dict:
    text = ctx.full_log
    shown = min(len(text), ctx.already_shown_chars)
    end = len(text) - shown
    start = max(0, end - max_chars)
    chunk = text[start:end]
    return {"earlier_log_excerpt": chunk or "(no earlier log content available)"}


def _tool_get_runner_failure_history(ctx: ToolContext) -> dict:
    from app.models import JobFailure  # local import avoids a circular import at module load time

    if ctx.runner_id is None:
        return {"error": "No runner_id associated with this job."}
    rows = ctx.db.execute(
        select(JobFailure.job_name, JobFailure.failure_reason, JobFailure.recorded_at)
        .where(JobFailure.runner_id == ctx.runner_id)
        .order_by(JobFailure.recorded_at.desc())
        .limit(10)
    ).all()
    return {
        "recent_failures_on_runner": [
            {"job_name": r.job_name, "failure_reason": r.failure_reason, "recorded_at": str(r.recorded_at)}
            for r in rows
        ]
    }


def _tool_get_job_diagnosis_history(ctx: ToolContext) -> dict:
    from app.models import JobFailure  # local import avoids a circular import at module load time

    rows = ctx.db.execute(
        select(JobFailure.ai_category, JobFailure.ai_root_cause, JobFailure.recorded_at)
        .where(JobFailure.project_id == ctx.project_id)
        .where(JobFailure.job_name == ctx.job_name)
        .where(JobFailure.ai_category != "")
        .order_by(JobFailure.recorded_at.desc())
        .limit(5)
    ).all()
    return {
        "past_diagnoses_for_this_job": [
            {"category": r.ai_category, "root_cause": r.ai_root_cause, "recorded_at": str(r.recorded_at)}
            for r in rows
        ]
    }


_TOOL_IMPLS = {
    "get_more_log_context": _tool_get_more_log_context,
    "get_runner_failure_history": _tool_get_runner_failure_history,
    "get_job_diagnosis_history": _tool_get_job_diagnosis_history,
}


async def diagnose_agentic(
    settings: Settings,
    db: Session,
    job_metadata: dict,
    full_log: str,
    recurrence: RecurrenceInfo,
    runner_id: int | None,
    project_id: str,
    client_factory=None,
) -> AIDiagnosis:
    """Reason/act/observe loop: the model may call tools before answering.

    Falls back to the single-shot diagnose() for providers/configurations
    that don't support tool calling.
    """
    if not settings.ai_reasoning_enabled:
        return _fallback("AI reasoning disabled (AI_REASONING_ENABLED=false)")

    log_excerpt = full_log[-settings.log_excerpt_max_chars :]

    if settings.ai_provider != "openai_compatible" or not settings.ai_agentic_tools_enabled:
        return await diagnose(settings, job_metadata, log_excerpt, recurrence, client_factory=client_factory)

    ctx = ToolContext(
        db=db,
        full_log=full_log,
        runner_id=runner_id,
        project_id=project_id,
        job_name=job_metadata.get("job_name", ""),
        already_shown_chars=settings.log_excerpt_max_chars,
    )
    prompt = build_prompt(job_metadata, log_excerpt, recurrence, tools_available=True)
    factory = client_factory or _default_client_factory
    failures: list[str] = []

    try:
        async with factory() as client:
            # Each model gets a fresh conversation; only HTTP/transport errors
            # (timeouts, 5xx, ...) move on to the next candidate.
            for model in settings.ai_model_candidates:
                try:
                    return await _run_agentic_loop(client, settings, model, ctx, prompt)
                except httpx.HTTPError as exc:
                    logger.error("Agentic AI request to model %s failed: %s", model, _describe_error(exc))
                    failures.append(f"{model}: {_describe_error(exc)}")
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        logger.error("Unexpected AI response shape during agentic loop: %s", exc)
        return _fallback(f"Could not parse model output: {exc}")

    return _fallback(f"AI request failed: {'; '.join(failures)}")


async def _run_agentic_loop(
    client: httpx.AsyncClient, settings: Settings, model: str, ctx: ToolContext, prompt: str
) -> AIDiagnosis:
    messages: list[dict] = [{"role": "user", "content": prompt}]
    headers = {"authorization": f"Bearer {settings.ai_api_key}", "content-type": "application/json"}
    tool_calls_made: list[str] = []

    for _ in range(settings.ai_agentic_max_tool_iterations):
        resp = await client.post(
            settings.ai_api_url,
            headers=headers,
            json={
                "model": model,
                "messages": messages,
                "tools": TOOL_SPECS,
                "tool_choice": "auto",
                "temperature": 0.1,
            },
            timeout=settings.ai_request_timeout_seconds,
        )
        resp.raise_for_status()
        data = resp.json()
        choice = data["choices"][0]
        message = choice["message"]

        requested_tool_calls = message.get("tool_calls")
        if not requested_tool_calls:
            diagnosis = _parse_diagnosis(message.get("content") or "")
            diagnosis.tool_calls_made = tool_calls_made
            return diagnosis

        messages.append(message)
        for tool_call in requested_tool_calls:
            name = tool_call["function"]["name"]
            try:
                args = json.loads(tool_call["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            impl = _TOOL_IMPLS.get(name)
            result = impl(ctx, **args) if impl else {"error": f"unknown tool '{name}'"}
            tool_calls_made.append(name)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.get("id", ""),
                    "name": name,
                    "content": json.dumps(result),
                }
            )

    logger.warning("AI agent exceeded %d tool-call iterations without concluding", settings.ai_agentic_max_tool_iterations)
    return _fallback("AI agent exceeded tool-call iteration limit without concluding")
