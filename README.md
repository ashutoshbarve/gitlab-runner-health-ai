# Runner Health Agent — Phase 1 + 2 (Monitoring + Diagnostics)

> For a standalone, non-technical introduction suitable for sharing or uploading
> to a chatbot, see [README_SHAREABLE.md](README_SHAREABLE.md). It covers
> Phases 1–2 and a brief Phase 3 outlook without credentials or internal addresses.

> See [ARCHITECTURE.md](ARCHITECTURE.md) for diagrams of the data flow, the
> agentic diagnosis loop, and the Kubernetes deployment topology.

## Public repository configuration

This repository is a sanitized source snapshot with a fresh Git history.
It does not include deployment credentials, webhook values, internal endpoints,
real project/group identifiers, logs, or databases.

- For Docker or local Python, copy `config.sample.env` to `.env` and configure
  the ignored copy, including your GitLab and AI connection settings.
- For Kubernetes, copy `k8s/configmap.sample.yaml` to `k8s/configmap.yaml` and
  `k8s/secret.sample.yaml` to `k8s/secret.yaml`. Both configured files are ignored.
- Keep the tracked templates generic. Never force-add private configuration,
  credentials, logs, or database exports.
- Tests use synthetic fixtures. Configuration is required before live monitoring
  or AI diagnosis can work.

Known limitation in this snapshot: an AI diagnosis returned as a JSON list
instead of the requested object can interrupt a collection cycle. This
publication cleanup preserves the current runtime behavior; it does not fix
that separately identified parsing issue.

Phases 1–2 of the [Runner Health Agent roadmap](Runner_Health_Agent_MVP.md):
a FastAPI service that polls GitLab for runner availability and failed CI
jobs, uses an LLM to actually diagnose *why* each job failed (not just a
fixed rule lookup), scores each runner's health, detects recurring/flaky
failures, and posts Teams alerts via a Power Automate workflow webhook.

## What's implemented

**Phase 1 - Monitoring**
- **Runner availability monitoring** — polls `GET /projects/:id/runners`,
  `GET /groups/:id/runners`, and/or `GET /runners/all` depending on
  `GITLAB_RUNNER_SCOPE`, stores status in SQLite, and records every status
  transition.
- **Build failure tracking** — polls `GET /projects/:id/jobs?scope[]=failed`
  and correlates each failure to the runner that executed it (GitLab returns
  `job.runner.id`). Under group scope with no explicit project list, projects
  are auto-discovered from the group (including subgroups).
- **Health scoring** — a simple rule engine (`app/health.py`) combines
  runner status + recent failure count into a 0–100 score with a plain-text
  recommended action.
- **Teams notifications** — Adaptive Cards posted on a runner status change
  (online ⇄ offline/stale/never_contacted), when a runner's score drops
  below threshold, and on every newly detected failed job.

**Phase 2 - Diagnostics (AI Reasoning Engine)**
- **Log analysis** — fetches each failed job's raw log (`GET
  /projects/:id/jobs/:job_id/trace`) and keeps the last `LOG_EXCERPT_MAX_CHARS`
  characters (where the actual error almost always is).
- **AI root cause diagnosis** (`app/ai_reasoning.py`) — sends the log excerpt +
  job metadata + recurrence context to an LLM (default: an internal
  OpenAI-compatible model broker running Llama 3.3 70B; a local Ollama
  instance is also supported for offline dev via `AI_PROVIDER=ollama`),
  which returns a free-form root cause, a category (Disk Full / Docker
  Failure / Vault Connectivity / Permission Issues / Network Connectivity /
  Script-Test Failure / Flaky-Intermittent / Other), a confidence score, and a
  recommended action — replacing the old fixed-rule lookup for failures.
  Fails soft: if the model is unreachable or returns unparseable output, the
  pipeline falls back to the raw GitLab `failure_reason` instead of breaking.
- **Agentic tool-calling** (`diagnose_agentic()`, `AI_AGENTIC_TOOLS_ENABLED`) —
  rather than always handing the model a fixed context blob, it's given three
  tools and decides for itself whether to use them before answering:
  `get_more_log_context` (earlier log lines beyond what was already shown),
  `get_runner_failure_history` (recent failures on the same runner, to spot
  runner-wide issues), and `get_job_diagnosis_history` (past diagnoses for
  this exact job, to spot recurring/flaky patterns). Confirmed working live:
  the model calls a tool and visibly raises its own confidence once it has
  evidence, rather than guessing. Falls back to the single-shot `diagnose()`
  path automatically for `AI_PROVIDER=ollama` or when disabled. Which tools
  (if any) were used for a given failure are recorded (`ai_tool_calls`) and
  shown in the Teams card.
- **Trend/recurrence analysis** (`app/trend_analyzer.py`) — counts how many
  times the exact same job has failed recently and how many failures the
  same runner has had overall, both as context fed into the AI prompt and via
  `GET /trends/recurring-jobs`.
- Teams job-failure cards now show the AI's root cause, category, confidence,
  recommended action, and (when used) which tools it called, alongside the
  raw GitLab `failure_reason`.

**Not included yet (deferred, see `app/collectors/docker_disk_collector.py`):**
Docker daemon / disk utilization monitoring. GitLab's API has no per-runner
disk/Docker telemetry, so this needs an on-runner push agent, SSH pull, or a
Prometheus node_exporter/cAdvisor scrape target — pick one and implement
`poll_docker_disk_metrics()` when ready.

> **Note on group scope**: auto-discovering projects under a large group
> means one job-failure poll per project, every cycle, each now also doing a
> log fetch + LLM call per new failure. For a group with hundreds of
> projects/failures this adds up — consider raising `POLL_INTERVAL_MINUTES`,
> `AI_REQUEST_TIMEOUT_SECONDS`, or setting `GITLAB_PROJECT_IDS` to a subset.


## Setup

### Option A — Docker (recommended)

1. Copy `config.sample.env` to `.env` and fill in `GITLAB_TOKEN`,
   `GITLAB_RUNNER_SCOPE`, `GITLAB_GROUP_IDS` or `GITLAB_PROJECT_IDS`, and
   `TEAMS_WEBHOOK_URL`. For AI diagnosis, also configure `AI_API_URL`,
   `AI_API_KEY`, and `AI_MODEL` (see below for requirements).
2. Build and start:
   ```powershell
   docker compose up -d --build
   ```
3. Check it's healthy and browse the API:
   ```powershell
   docker compose ps
   curl http://localhost:8000/health
   ```
   Docs: http://localhost:8000/docs

The SQLite database is stored in the named volume `runner_health_data`
(mounted at `/app/data` in the container), so it survives
`docker compose down`/recreate — only `docker compose down -v` wipes it.
`docker-compose.yml` pins `DATABASE_URL` to that path regardless of what's in
`.env`, so don't override `DATABASE_URL` unless you also adjust the volume
mount to match.

Useful commands:
```powershell
docker compose logs -f          # tail logs
docker compose restart          # restart after editing .env
docker compose down             # stop + remove container (keeps data volume)
```

### Option B — Kubernetes

Manifests are in [`k8s/`](k8s): `namespace.yaml`, `configmap.sample.yaml`,
`pvc.yaml`, `deployment.yaml`, `service.yaml`, `kustomization.yaml`, plus
`secret.sample.yaml` as a template (the real `secret.yaml` is gitignored).

1. **Build and push the image to a registry your cluster can actually pull
   from.** A plain local `docker build` tag is *not* enough for most
   clusters — including Docker-Desktop's built-in Kubernetes on managed
   corporate machines, where a **Registry Access Management** policy
   may block ad-hoc/local registries. Push to
   whatever registry is actually approved/reachable for your target cluster,
   e.g. Amazon ECR for an EKS cluster:
   ```powershell
   docker build -t <your-registry>/runner-health-agent:latest .
   docker push <your-registry>/runner-health-agent:latest
   ```
2. Update the `image:` field in `k8s/deployment.yaml` to match.
3. Create private configuration from the templates:
   ```powershell
   Copy-Item k8s\configmap.sample.yaml k8s\configmap.yaml
   Copy-Item k8s\secret.sample.yaml k8s\secret.yaml
   ```
4. Configure GitLab URL, runner scope, project/group IDs, and AI endpoint/model
   in the private `k8s/configmap.yaml`. Set the GitLab token, Teams workflow URL,
   and AI API key in the private `k8s/secret.yaml`. Do not put real values into
   either tracked sample file.
5. Apply the manifests, including the private configuration:
   ```powershell
   kubectl apply -k k8s/
   kubectl -n runner-health-agent get pods,pvc,svc
   ```
6. Reach the API (no Ingress defined yet — use port-forward for now):
   ```powershell
   kubectl -n runner-health-agent port-forward svc/runner-health-agent 8000:8000
   curl http://localhost:8000/health
   ```

Notes:
- `strategy: Recreate` on the Deployment avoids two pods writing the same
  SQLite file concurrently (acceptable for a single-replica MVP; move to
  Postgres before scaling to multiple replicas).
- The PVC (`runner-health-agent-data`, `ReadWriteOnce`, 1Gi) keeps the SQLite
  DB across pod restarts/recreates, same as the Docker volume in Option A.
- After changing the Secret or ConfigMap, run
  `kubectl -n runner-health-agent rollout restart deployment/runner-health-agent`
  to pick up the change (pods don't auto-reload env vars).

### Option C — Local Python (venv)

1. Create and activate a virtualenv, then install dependencies:
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```
2. Copy `config.sample.env` to `.env` and fill in the same values as above.
3. Run the service:
   ```powershell
   .\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
   ```
   On startup it creates `runner_health.db` (SQLite) and, if GitLab config is
   present, starts the background scheduler (runs immediately, then every
   `POLL_INTERVAL_MINUTES`).

### Required config values (Options A and C use `.env`; Option B uses the ConfigMap/Secret)

- `GITLAB_TOKEN` — a Personal Access Token. Scope/role needed depends on
  `GITLAB_RUNNER_SCOPE`:
  | Scope | Runner-listing endpoint | Token scope | Role required |
  |---|---|---|---|
  | `project` | `GET /projects/:id/runners` | `read_api` | Maintainer or Auditor on the project |
  | `group` | `GET /groups/:id/runners` | `manage_runner` (not just `read_api`) | Owner or Auditor in the group |
  | `instance` | `GET /runners/all` | `read_api` | GitLab instance administrator |

  Job/failure listing (`GET /projects/:id/jobs`) only needs `read_api` +
  Reporter role, regardless of scope.
- `GITLAB_RUNNER_SCOPE` — `project`, `group`, or `instance`. Use `group` to
  pull in every runner under a group (plus its ancestor groups) instead of
  listing projects one by one — useful when a group has many runners.
- `GITLAB_GROUP_IDS` — comma-separated group IDs (used when scope is
  `group`). Find a group's ID on its group home page, same place as a
  project ID.
- `GITLAB_PROJECT_IDS` — comma-separated project IDs. Required when scope is
  `project`. Optional under `group` scope: leave blank to auto-discover every
  project under the group(s) for job-failure polling, or set it to restrict
  failure tracking to a subset of projects.
- `TEAMS_WEBHOOK_URL` — your Power Automate "Post card in a chat or
  channel when a webhook request is received" workflow URL. Leave blank
  to log alerts to console only.
- `AI_REASONING_ENABLED` — set to `false` to skip the LLM call entirely and
  fall back to raw GitLab `failure_reason` only (useful if no model server
  is available, or to save compute).
- `AI_PROVIDER` — `openai_compatible` (default) or `ollama`.
  - `openai_compatible`: `AI_API_URL` (full `/v1/chat/completions`-style
    URL), `AI_API_KEY` (secret - Secret/`.env` only, never a ConfigMap or
    committed file), `AI_MODEL` (e.g. `llama-3.3-70b`).
  - `ollama`: `OLLAMA_URL` / `OLLAMA_MODEL` for a local/self-hosted Ollama
    instance. From a container/pod on Docker Desktop,
    `http://host.docker.internal:11434` reaches a model running on your
    host machine - useful for offline dev, but a single point of failure
    for anything beyond that.
- `AI_AGENTIC_TOOLS_ENABLED` — set to `false` to force the single-shot
  path even on `openai_compatible` (e.g. if your broker doesn't support
  tool calling, or to reduce latency/cost).
- `AI_AGENTIC_MAX_TOOL_ITERATIONS` — hard cap on tool-call round-trips per
  diagnosis, so a model that keeps calling tools without concluding can't
  loop forever (falls back to the raw `failure_reason` if hit).
- `TREND_WINDOW_DAYS` / `RECURRENCE_ALERT_THRESHOLD` — how far back
  `/trends/recurring-jobs` looks, and how many repeats of the same job
  failing counts as "recurring" context fed into the AI prompt.

## API

| Endpoint | Description |
|---|---|
| `GET /runners` | All known runners with current status/health score, worst first |
| `GET /runners/{id}` | Single runner detail |
| `GET /runners/{id}/history` | Status transition history for a runner |
| `GET /failures` | Recently recorded failed jobs, including AI diagnosis fields (newest first) |
| `GET /health` | Fleet summary: counts by status, average score, runners below threshold |
| `GET /trends/recurring-jobs` | (project, job) pairs that have failed repeatedly within `TREND_WINDOW_DAYS` |
| `POST /failures/{id}/rediagnose` | Re-run AI diagnosis for one failure (re-fetches the job log from GitLab fresh). `?notify=true` also posts a fresh Teams card |
| `POST /failures/rediagnose-errors` | Bulk re-run diagnosis for every failure whose last AI attempt errored (`ai_error` non-empty) - the fix-forward path after a bug fix in the AI pipeline. `?limit=` caps how many, `?notify=true` posts Teams cards |

Interactive docs at `http://127.0.0.1:8000/docs`.

## Teams alert payload

Alerts are posted directly as **Adaptive Cards** to `TEAMS_WEBHOOK_URL`,
wrapped in the `type: message` / `attachments` envelope that the "When a
Teams webhook request is received" trigger expects
([docs](https://learn.microsoft.com/connectors/teams/#microsoft-teams-webhook)) -
no field-mapping step needed in the Power Automate flow designer, the card
renders as-is:

```jsonc
// Runner status / health-score alert
{
  "type": "message",
  "attachments": [
    {
      "contentType": "application/vnd.microsoft.card.adaptive",
      "content": {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": [
          { "type": "TextBlock", "text": "🚨 Runner Health Alert", "weight": "Bolder", "size": "Medium", "color": "attention" },
          { "type": "FactSet", "facts": [
            { "title": "Runner", "value": "gitlab-runner-prod-05 (#123)" },
            { "title": "Status", "value": "offline" },
            { "title": "Health score", "value": "42/100" },
            { "title": "Issues", "value": "Runner offline" },
            { "title": "Recommended action", "value": "Check the runner host/VM is powered on and the gitlab-runner service is running" },
            { "title": "Confidence", "value": "90%" },
            { "title": "Timestamp", "value": "2026-11-16T08:15:00+00:00" }
          ]}
        ]
      }
    }
  ]
}

// Job failure alert additionally includes an Action.OpenUrl button linking
// to the job's GitLab web_url, and facts for Project/Job/Stage/Ref/Failure
// reason/Runner/Timestamp instead.
```

Every attempt (success or failure) is also logged to the `alert_log` table
(storing the full card payload sent) for troubleshooting.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ -v
```

Covers the health-scoring rules (`app/health.py`), AI-reasoning prompt
building + response parsing/fallback behavior for both providers
(`app/ai_reasoning.py`, mocked HTTP - no real model server required to run
tests), and trend recurrence aggregation (`app/trend_analyzer.py`, in-memory
SQLite). No network or real GitLab/LLM access required.

## Next steps (per roadmap)

- **Phase 1.5**: pick a Docker/disk metrics collection approach and
  implement `app/collectors/docker_disk_collector.py`.
- **Phase 3 (Assisted Remediation)**: approval-gated actions (runner
  restart, Docker/workspace cleanup) triggered from the AI diagnosis.
- **Phase 4 (Autonomous Operations)**: self-healing runners, predictive
  failure detection.
- Possible Phase 2 refinements: swap providers/models by changing
  `AI_PROVIDER`/`AI_API_URL`/`AI_MODEL` (or `OLLAMA_URL`/`OLLAMA_MODEL`), or
  extend `app/ai_reasoning.py` for a different provider's API shape
  provider's API shape.
