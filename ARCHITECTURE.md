# Architecture — Runner Health Agent (Phase 1 + 2)

This reflects the current implemented state: Phase 1 (Monitoring) + Phase 2
(Diagnostics, including agentic tool-calling). See
[Runner_Health_Agent_MVP.md](Runner_Health_Agent_MVP.md) for the full
roadmap (Phases 3–4 not yet built).

## System / data-flow architecture

```mermaid
flowchart TB
    subgraph External["External Systems"]
        GL["Configured GitLab instance<br/>Groups / Projects / Runners / Jobs / Trace APIs"]
        Teams["Microsoft Teams<br/>(Power Automate workflow webhook)"]
        Broker["Model Broker<br/>Llama 3.3 70B<br/>/v1/chat/completions"]
        Ollama["Ollama (local, optional)<br/>offline-dev fallback"]
    end

    subgraph App["runner-health-agent (FastAPI service)"]
        Sched["APScheduler<br/>(every POLL_INTERVAL_MINUTES)"]

        subgraph Collectors["Collectors"]
            RC["Runner Collector<br/>app/collectors/runner_collector.py"]
            JC["Job Collector<br/>app/collectors/job_collector.py"]
        end

        Health["Health Scoring (rule-based)<br/>app/health.py"]
        Trend["Trend / Recurrence Analyzer<br/>app/trend_analyzer.py"]

        subgraph AIEngine["AI Reasoning Engine — app/ai_reasoning.py"]
            direction TB
            Diag["diagnose_agentic()<br/>reason → act → observe loop"]
            Tools["Tools the model may call:<br/>• get_more_log_context<br/>• get_runner_failure_history<br/>• get_job_diagnosis_history"]
            Diag <--> Tools
        end

        Notifier["Teams Notifier<br/>app/notifier.py<br/>(Adaptive Cards)"]
        DB[("SQLite<br/>runners / job_failures /<br/>runner_status_events / alert_log")]

        API["FastAPI HTTP API<br/>/runners /failures /health /trends"]
    end

    GL -- "runner status, failed jobs, job trace" --> RC
    GL -- "failed jobs, job trace" --> JC
    Sched --> RC
    Sched --> JC

    RC --> Health --> DB
    JC --> Trend --> Diag
    JC -- "log excerpt + metadata" --> Diag
    Diag -- "tool calls query" --> DB
    Diag -. "primary" .-> Broker
    Diag -. "AI_PROVIDER=ollama" .-> Ollama
    Diag --> DB

    RC --> Notifier
    JC --> Notifier
    Notifier -- "Adaptive Card POST" --> Teams

    DB --> API
    Health -.->|"health_score, issues"| Notifier
```

## Diagnosis flow detail (the agentic loop)

```mermaid
sequenceDiagram
    participant JC as Job Collector
    participant Trend as Trend Analyzer
    participant AI as AI Reasoning Engine
    participant Broker as Model Broker (LLM)
    participant DB as SQLite

    JC->>GitLab: GET failed jobs, then GET job trace
    JC->>Trend: compute_recurrence(project, job, runner)
    Trend->>DB: query recent job_failures
    Trend-->>JC: RecurrenceInfo
    JC->>AI: diagnose_agentic(log, metadata, recurrence)
    AI->>Broker: chat.completions (prompt + tool specs)
    alt model requests a tool
        Broker-->>AI: tool_calls: get_runner_failure_history
        AI->>DB: query failures for this runner_id
        DB-->>AI: recent failures list
        AI->>Broker: chat.completions (+ tool result)
        Broker-->>AI: final JSON answer (higher confidence)
    else model answers directly
        Broker-->>AI: final JSON answer
    end
    AI-->>JC: AIDiagnosis(root_cause, category, confidence, action, tools_used)
    JC->>DB: store JobFailure + diagnosis
    JC->>Notifier: send_job_failure_alert()
```

## Kubernetes deployment topology

```mermaid
flowchart TB
    subgraph Cluster["Kubernetes cluster (namespace: runner-health-agent)"]
        CM["ConfigMap<br/>runner-health-agent-config<br/>(GitLab scope, AI provider/model, thresholds)"]
        Sec["Secret<br/>runner-health-agent-secrets<br/>(GITLAB_TOKEN, TEAMS_WEBHOOK_URL, AI_API_KEY)<br/>— not committed, k8s/secret.yaml gitignored"]
        Dep["Deployment<br/>runner-health-agent<br/>(1 replica, strategy: Recreate)"]
        Pod["Pod<br/>FastAPI + APScheduler"]
        PVC["PersistentVolumeClaim<br/>runner-health-agent-data (1Gi, RWO)"]
        Svc["Service (ClusterIP)<br/>runner-health-agent :8000"]

        CM --> Dep
        Sec --> Dep
        Dep --> Pod
        Pod --> PVC
        Svc --> Pod
    end

    Dev["kubectl port-forward"] --> Svc
    Pod -- "HTTPS" --> GLExt["Configured GitLab instance"]
    Pod -- "HTTPS" --> BrokerExt["Configured AI endpoint"]
    Pod -- "HTTPS" --> TeamsExt["Power Automate webhook"]

    style Sec fill:#f8d7da,stroke:#c0392b
    style PVC fill:#d4edda,stroke:#27ae60
```

### Deployment notes
- `k8s/deploy.ps1` is the one-shot deploy path for the local Docker-Desktop
  cluster: build → import image directly into the node's containerd (works
  without requiring a registry pull) → apply all manifests (including
  `secret.yaml`) → restart → wait for rollout.
- Copy `k8s/configmap.sample.yaml` to `k8s/configmap.yaml` and
  `k8s/secret.sample.yaml` to `k8s/secret.yaml` before deployment.
  Both configured files are private and gitignored.
- For a real cluster (e.g. EKS), the image would instead be pushed to an
  approved registry (ECR/Artifactory) and `deployment.yaml`'s `image:`
  updated accordingly — the local-import step is a Docker-Desktop-specific
  workaround, not a general deployment method.
- SQLite + a single pod (`strategy: Recreate`) is an MVP simplification;
  moving to Postgres would be needed before running multiple replicas.
