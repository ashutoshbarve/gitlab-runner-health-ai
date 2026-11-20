# Runner Health Agent - Agentic Ops MVP

## Overview

### Objective

Build an AI-powered Runner Health Agent that continuously monitors GitLab runners, detects issues proactively, diagnoses root causes, recommends remediation actions, and eventually performs safe autonomous recovery actions.

The goal is to reduce build failures, decrease MTTR (Mean Time To Resolution), and minimize manual operational effort.

---

# Problem Statement

Current workflow:

```text
Build Fails
    ↓
Engineer Investigates
    ↓
Checks Logs
    ↓
Identifies Root Cause
    ↓
Performs Remediation
```

Target workflow:

```text
Issue Detected
    ↓
AI Agent Investigates
    ↓
Root Cause Identified
    ↓
Remediation Suggested
    ↓
(Optional) Auto-Remediation
```

---

# Use Cases

## Runner Availability Monitoring
- Runner online/offline status
- Last heartbeat
- Runner registration status
- Runner tags availability

## Disk Health Monitoring
- Monitor disk utilization
- Monitor Docker storage consumption
- Trigger cleanup recommendations or actions

## Docker Health Monitoring
- Docker daemon status
- Image cache utilization
- Stopped containers
- Volume consumption

## Workspace Health Monitoring
- Corrupted workspaces
- Stale repositories
- Permission issues
- Detached HEAD states

## Build Failure Correlation
Automatically categorize failures:
- Disk Full
- Docker Failure
- Vault Connectivity
- Permission Issues
- Network Connectivity

---

# AI Agent Responsibilities

## Observe
Collect telemetry from:
- GitLab API
- Docker Engine
- Linux OS Metrics
- Runner Logs
- System Logs

## Detect
Identify:
- Health degradation
- Resource exhaustion
- Service failures
- Configuration drift

## Diagnose
Generate root cause analysis with confidence score.

## Recommend
Provide remediation guidance.

## Remediate
Future capability:
- Docker cleanup
- Workspace cleanup
- Runner restart
- Cache cleanup

---

# Architecture

```text
Runner Health Agent
│
├── Health Collector
├── Docker Analyzer
├── GitLab Analyzer
├── Log Analyzer
├── Rule Engine
├── AI Reasoning Engine
├── Notification Service
└── Remediation Engine
```

---

# Technology Stack

## MVP
- Python 3.12
- FastAPI
- GitLab API
- Docker SDK for Python
- Teams Webhook
- SQLite
- APScheduler

## Advanced
- Azure OpenAI / GPT-5
- Semantic Kernel
- PostgreSQL
- Prometheus
- Grafana
- OpenTelemetry

---

# Teams Notification Example

```text
🚨 Runner Health Alert

Runner: gitlab-runner-prod-05
Health Score: 42/100

Issues:
- Disk usage 95%
- Docker cache 48 GB
- 21 stopped containers

Recommended Action:
Docker Cleanup

Confidence: 99%
```

---

# Roadmap

## Phase 1 – Monitoring ✅ Implemented
- ~~Runner dashboard~~ → JSON API instead (no UI planned for MVP)
- ✅ Availability monitoring (project/group/instance scope)
- ⏸ Disk monitoring — deferred (needs an on-runner agent/SSH-pull/Prometheus decision; see `app/collectors/docker_disk_collector.py`)
- ⏸ Docker monitoring — deferred, same reason as above
- ✅ Teams notifications (Adaptive Cards, full width)

## Phase 2 – Diagnostics ✅ Complete
- ✅ Log analysis (pulls actual job trace from GitLab, not just metadata)
- ✅ Failure classification (AI-assigned category: Disk Full / Docker Failure /
  Vault Connectivity / Permission Issues / Network Connectivity /
  Script-Test Failure / Flaky-Intermittent / Other)
- ✅ Root cause detection — via an LLM (Llama 3.3 70B through an internal
  OpenAI-compatible model broker), **with agentic tool-calling**: the model
  can call tools itself (fetch more log context, check the runner's recent
  failure history, check past diagnoses for this job) before answering,
  rather than only reasoning over a fixed context blob
- ✅ Trend analysis (`/trends/recurring-jobs` — recurring/flaky job detection)

**Closed out 2026-11-18**: found and fixed a real production bug (unescaped
Windows file-path backslashes breaking JSON parsing on .NET/MSBuild
failures), added a retroactive re-diagnosis mechanism
(`POST /failures/{id}/rediagnose`, `POST /failures/rediagnose-errors`) to
fix any future AI-pipeline bugs without waiting for new failures, and
verified both affected real failures now produce clean, accurate
diagnoses. 38 automated tests passing.

## Phase 3 – Assisted Remediation ⏳ Not started (next up)
- Approval-based actions
- Runner restart
- Docker cleanup
- Workspace cleanup

## Phase 4 – Autonomous Operations ⏳ Not started
- Self-healing runners
- Predictive failure detection
- Automated RCA generation

---

# Current Implementation Status

Full code, docs, and diagrams live in [this repository](.) (this
file's own directory). See [README.md](README.md) for complete setup/config
reference and [ARCHITECTURE.md](ARCHITECTURE.md) for diagrams.

**Implemented capabilities (not a live deployment report):**
- GitLab runner monitoring with configurable project, group, or instance scope
- Project discovery under configured GitLab groups
- AI diagnosis with optional read-only investigation tools
- Teams Adaptive Cards with full-width rendering
- One-shot deploy script (`k8s/deploy.ps1`) handles build → image import →
  manifest apply → restart → rollout wait
- Automated tests cover health scoring, AI reasoning including the agentic
  loop, fallback and JSON-repair behavior, trend analysis, Adaptive Card
  building, project resolution logic, and re-diagnosis — no network/live
  credentials required to run them.
- This public repository starts from a sanitized snapshot without private
  deployment history, identifiers, credentials, or operational records.

**Known limitations / honest caveats:**
- Docker/disk monitoring not implemented (needs an architecture decision —
  see Next Plan)
- SQLite + single replica only (fine for MVP; would need Postgres + HA
  before scaling to multiple pods)
- The agentic AI reasoning only diagnoses — it does not take any action yet
  (that's Phase 3)
- Recurring-failure data exists (`/trends/recurring-jobs`) but doesn't
  proactively push a Teams alert yet — pull-only via the API today
- "AI confidence" is a self-reported number from the model, not an
  independently verified/calibrated probability

---

# Next Plan

1. **Phase 3 (Assisted Remediation)** — the natural next step now that
   diagnosis is solid: let the agent *propose* a remediation (e.g. "restart
   this runner", "clear Docker cache") and execute it only after human
   approval (e.g. a Teams Adaptive Card with an Action.Submit button, or a
   `/remediate/{failure_id}` API call a human triggers).
2. **Proactive recurring-failure alerts** — turn `/trends/recurring-jobs`
   from pull-only into a push alert (e.g. "job X has failed 5 times this
   week") so flaky/chronic issues surface without someone checking the API.
3. **Docker/disk monitoring decision (Phase 1.5)** — pick one of: a
   lightweight push agent on each runner, SSH-pull from the central
   service, or scraping existing Prometheus node_exporter/cAdvisor if
   already deployed on the runner fleet.
4. **Registry hardening** — the local deploy script imports images into
   Docker Desktop's containerd store. For other clusters, push to an
   approved registry and configure the deployment image accordingly.
5. **Reduce single points of failure** — the AI reasoning engine currently
   depends on one model broker endpoint with no retry/circuit-breaker
   beyond the existing fail-soft fallback; worth hardening if this becomes
   a production dependency.

---

# How to Run It

Full detail (all config options, permission requirements, troubleshooting)
is in [README.md](README.md) — this is the condensed version.

## Option A — Docker Compose (simplest, fully self-contained)

```powershell
cd gitlab-runner-health-ai
Copy-Item config.sample.env .env
# edit .env: GITLAB_TOKEN, GITLAB_RUNNER_SCOPE, GITLAB_GROUP_IDS or
# GITLAB_PROJECT_IDS, TEAMS_WEBHOOK_URL, AI_API_URL, AI_API_KEY, AI_MODEL
docker compose up -d --build
docker compose logs -f
```

## Option B — Kubernetes (local Docker Desktop one-shot script)

```powershell
cd gitlab-runner-health-ai\k8s
Copy-Item configmap.sample.yaml configmap.yaml
Copy-Item secret.sample.yaml secret.yaml
# edit secret.yaml: GITLAB_TOKEN, TEAMS_WEBHOOK_URL, AI_API_KEY
# edit configmap.yaml: GitLab URL, project/group IDs, AI endpoint and model
# both configured files are private and gitignored; keep samples generic
.\deploy.ps1
```
`deploy.ps1` builds the image, imports it into the cluster node, applies
every manifest (namespace/configmap/secret/pvc/deployment/service), restarts
the deployment, and waits for it to come back healthy — one command, no
manual steps.

To reach the API afterward:
```powershell
kubectl -n runner-health-agent port-forward svc/runner-health-agent 8000:8000
```

## Endpoints and expected output

All identifiers, runner descriptions, addresses, timestamps, and counts in
the examples below are synthetic, not live operational records.

Use `Invoke-RestMethod` in PowerShell (not `curl`, which is aliased to
`Invoke-WebRequest` and won't auto-parse JSON).

### `GET /`
```powershell
Invoke-RestMethod http://localhost:8000/
```
```json
{ "service": "runner-health-agent", "phase": "1-monitoring", "docs": "/docs" }
```

### `GET /runners`
All known runners, worst health score first.
```powershell
Invoke-RestMethod http://localhost:8000/runners | Select-Object -First 3 id, description, status, health_score, issues
```
```json
[
  {
    "id": 1,
    "description": "example-runner",
    "status": "online",
    "health_score": 90,
    "issues": ["2 failed job(s) in the lookback window"],
    "contacted_at": "2026-11-17T06:40:12+00:00",
    "updated_at": "2026-11-17T06:40:15+00:00"
  }
]
```

### `GET /runners/{id}`
Single runner detail — same shape as one item above.

### `GET /runners/{id}/history`
Status transition history for that runner.
```json
[
  { "previous_status": "online", "new_status": "offline", "changed_at": "2026-11-16T09:12:00+00:00", "notified": true }
]
```

### `GET /failures`
Recently recorded failed jobs, newest first, with AI diagnosis fields.
```powershell
Invoke-RestMethod http://localhost:8000/failures | Select-Object id, job_name, ai_category, ai_confidence, ai_root_cause, ai_tool_calls
```
```json
[
  {
    "id": 200,
    "project_id": "100",
    "runner_id": 1,
    "job_name": "unit-test-job",
    "stage": "test",
    "ref": "master",
    "failure_reason": "script_failure",
    "web_url": "https://gitlab.example.com/example/project/-/jobs/200",
    "ai_root_cause": "The job failed due to a script error unrelated to infrastructure.",
    "ai_category": "Script/Test Failure",
    "ai_confidence": 75,
    "ai_recommended_action": "Review the failing test/assertion in the job log.",
    "ai_error": "",
    "ai_tool_calls": [],
    "recurrence_count": 1
  }
]
```
Failures recorded **before** Phase 2 was deployed show blank `ai_*` fields
(not backfilled, by design).

### `GET /health`
Fleet summary.
```json
{
  "total_runners": 10,
  "online": 8,
  "offline": 1,
  "stale": 1,
  "average_score": 87,
  "runners_below_threshold": [ /* RunnerOut objects, same shape as /runners */ ]
}
```

### `GET /trends/recurring-jobs`
(project, job) pairs that failed repeatedly within `TREND_WINDOW_DAYS`
(default 7), optional `?min_count=` and `?limit=` query params.
```json
[
  { "project_id": "100", "job_name": "example-build", "failure_count": 2, "last_failed_at": "2026-11-17T03:15:07+00:00" }
]
```

### `GET /docs`
Interactive Swagger UI for all of the above — open in a browser at
`http://localhost:8000/docs`.

## How to validate it's actually working
1. `kubectl -n runner-health-agent get pods` → `1/1 Running`
2. `kubectl -n runner-health-agent logs deployment/runner-health-agent --tail=30 | Select-String "gitlab|Collection cycle"` → `200 OK` calls, not `401`/`403`
3. Hit `/health` and `/runners` → non-empty, real data
4. Wait for (or trigger) a real CI job failure → check `/failures` for a new row with populated `ai_*` fields, and check the Teams channel for the Adaptive Card
5. `ai_tool_calls` being non-empty on a diagnosis is the concrete proof of agentic behavior (the model chose to investigate further rather than just answer from the initial context)

---

# Success Metrics

- Reduce MTTR by 50%
- Reduce runner incidents by 40%
- Reduce troubleshooting effort by 60%
- Runner availability > 99.5%
- Detection time < 1 minute

---

# Future Enhancements

- Predict disk exhaustion
- Automated RCA generation
- Jira integration
- ServiceNow integration
- Kubernetes runner support
- Capacity forecasting
- Historical failure knowledge base
