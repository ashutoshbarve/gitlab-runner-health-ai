# Runner Health Agent

## Plain-language project overview

**Runner Health Agent monitors GitLab runners and failed CI/CD jobs, investigates likely failure causes, and sends useful alerts to Microsoft Teams.**

Instead of only reporting "the build failed," it helps engineers understand what failed, why it may have failed, and what to check next.

This is a standalone introduction for teammates, stakeholders, and chatbot-assisted questions. It covers implemented Phases 1 and 2, followed by a brief introduction to planned Phase 3. It intentionally excludes credentials, internal addresses, real project identifiers, and detailed code or deployment instructions.

**Scope:** This describes the project implementation reviewed on 19 November 2026, not a live operational status report. Features available in a particular deployment depend on its version and configuration.

## 1. Why this project exists

When a CI/CD job fails, an engineer usually has to open GitLab, read logs, check the runner, compare previous failures, and decide what to do. Repeated failures can make this investigation time-consuming.

Runner Health Agent brings these steps together:

- Watches runner availability.
- Records failed jobs and links them to their runner when GitLab provides that information.
- Uses logs and failure history to suggest a likely root cause.
- Sends an actionable summary to Teams.
- Keeps records that engineers can query later.

The intended benefits are faster investigation, better visibility, and less repetitive manual work. These are goals, not measured performance guarantees.

## 2. Basic terms

| Term | Meaning |
|---|---|
| GitLab | The platform hosting source code and running CI/CD pipelines. |
| CI/CD pipeline | A sequence of automated steps such as building, testing, and deploying software. |
| Job | One step in a pipeline, such as compiling an application or running tests. |
| Runner | The worker that executes GitLab jobs. |
| Job log / trace | The output produced while a job runs, including errors. |
| Artifact | A file produced by a job that another job may need, such as a compiled package. |
| Root cause diagnosis | An explanation of the likely underlying reason for a failure. |
| LLM | A large language model used here to interpret logs and suggest explanations. |
| API | An interface through which software or an engineer can request information. |

## 3. Current scope at a glance

| Phase | Purpose | Status and boundary |
|---|---|---|
| Phase 1: Monitoring | Detect runner availability changes and failed jobs; score health and notify engineers. | Implemented for GitLab-based monitoring. Direct disk and Docker monitoring remain deferred. |
| Phase 2: Diagnostics | Analyze failures, use relevant history, and recommend next steps. | Implemented. Diagnosis is advisory, not automatic repair. |
| Phase 3: Assisted remediation | Execute selected recovery actions after human approval. | Planned; not implemented. |

## 4. Phase 1: Monitoring

### Runner availability

The service periodically asks GitLab for runner information. It records states such as online, offline, stale, and never contacted, and keeps a history of observed status changes.

Monitoring can be configured around selected projects, groups, or the GitLab instance, subject to access permissions. Under group scope, projects can be discovered automatically, including projects in subgroups. Instance-wide runner monitoring does not automatically mean every project's failed jobs are collected; job tracking still needs a configured or discovered project scope.

This is scheduled polling, not instantaneous event streaming. Detection time depends on the polling interval and how long collection takes.

### Failed-job tracking

The agent collects recently failed jobs, records useful details, and avoids recording the same job ID repeatedly. Where available, it records the runner that executed the job.

A failed job does not necessarily mean a broken runner. Application code, tests, permissions, network services, or missing build artifacts can also be responsible.

### Runner health score

Each runner receives a rule-based score from 0 to 100. Higher scores indicate fewer problems in the available signals.

The score combines GitLab-reported runner status with recent recorded job failures. It is not AI-generated, a hardware measurement, or a probability that the runner will succeed. It helps prioritize investigation rather than prove the cause of an issue.

### Teams notifications

The service uses a Microsoft Power Automate workflow webhook to send structured Teams messages called Adaptive Cards.

Alerts cover:

- Observed runner transitions into problem states and recovery to online.
- A runner's health score entering the configured below-threshold state.
- Newly detected failed jobs.

Runner alerts include status, score, issues, and a suggested next step. Failed-job alerts can include AI diagnosis details and a link back to the GitLab job.

If Teams is not configured, alerts are logged instead. Notification attempts and delivery errors are recorded.

## 5. Phase 2: Diagnostics

### Log-based investigation

For each newly recorded failed job, the agent fetches its GitLab log. It initially provides the AI with a limited excerpt from the end of the log, along with job details and recent failure counts.

The AI produces:

- **Likely root cause:** a short explanation of what went wrong.
- **Category:** the type of failure.
- **Confidence:** the model's reported confidence from 0 to 100.
- **Recommended action:** a suggested next step for an engineer.

Supported categories include disk full, Docker failure, Vault connectivity, permission issues, network connectivity, missing dependency, script/test failure, flaky/intermittent, and other.

These are diagnostic labels. For example, a "Disk Full" diagnosis may come from an error in the job log; it does not mean the agent directly measured the runner's disk usage.

### What makes the investigation "agentic"?

When a compatible AI provider and tool-calling configuration are used, the model can request additional evidence before answering:

1. Earlier log output beyond the initial excerpt.
2. Recent failures on the same runner.
3. Previous diagnoses for the same project and job name.

This lets it compare a single-job problem with a possible runner-wide issue. Tool use is recorded and can appear in the Teams alert. The investigation has a configured iteration limit.

**These tools only gather diagnostic information. They do not execute shell commands, restart runners, or change infrastructure.**

With tool calling disabled, or when using the supported Ollama path, the model analyzes the supplied context without this tool-driven investigation.

### Recurring-failure analysis

The agent counts repeated failures for the same project/job and failures associated with the same runner over a configured time window. This history supports AI diagnosis and is available through a recurring-jobs API.

Repeated failures are a signal for investigation, not proof that a test is flaky. The current recurrence analysis counts failures; it does not establish a pass/fail success rate. There is no separate proactive Teams alert specifically for recurring-failure trends.

### Missing-dependency investigation

The current implementation also has targeted analysis for GitLab failures caused by unavailable upstream artifacts. Where accessible, it examines pipeline jobs and CI configuration to identify possible upstream causes, such as expired artifacts or an upstream job that did not succeed.

This can help explain failures even when the affected job has little or no log output. The detail available depends on what GitLab returns and whether the relevant configuration can be read.

### When AI analysis is unavailable

Monitoring and failure recording do not require a successful AI diagnosis. If AI is disabled, unreachable, or returns unusable output, the service records the analysis problem and retains the original GitLab failure reason for manual investigation. Available dependency analysis can still provide useful context.

Engineers can request a fresh diagnosis for a stored failure, or retry failures whose previous AI analysis errored. Re-diagnosis can optionally send an updated Teams notification. It does not rerun the GitLab job.

## 6. How it is implemented

The implementation is a central Python service, rather than software installed on every runner.

```text
GitLab runner status, failed jobs, and job logs
                     |
                     v
          Scheduled data collection
                     |
                     v
     Health scoring and failure-history analysis
                     |
                     v
       AI diagnosis for failed jobs
                     |
                     v
       Stored records and Teams alerts
                     |
                     v
        Engineer reviews and takes action
```

| Component | Role |
|---|---|
| Python and FastAPI | Run the service and expose its HTTP API. |
| APScheduler | Trigger collection at configured intervals. |
| GitLab API | Supply runner information, failed jobs, logs, and relevant pipeline details. |
| Rule-based scoring | Turn runner status and recent failures into a health score. |
| AI integration | Use a configured OpenAI-compatible endpoint or Ollama to analyze failures. |
| SQLite | Store runner information, status history, failed-job records, diagnoses, log excerpts, and alert records. |
| Teams and Power Automate | Deliver readable operational notifications. |
| Docker / Kubernetes | Provide deployment options; local Python execution is also supported. |

The current deployment design uses a single service instance with persistent SQLite storage. It is an MVP design, not a multi-replica high-availability architecture.

### How people use it

- **Teams:** receive alerts and follow the link to the original GitLab job.
- **HTTP API:** inspect runners, health summaries, failure diagnoses, status history, and recurring jobs.
- **Interactive API documentation:** explore available requests through FastAPI's documentation page.

There is no dedicated graphical dashboard or built-in conversational chatbot. Uploading this document to a separate chatbot helps explain the project; it does not connect that chatbot to the running service.

### What is needed to run it?

At a high level, an operator needs:

- Access to GitLab and permission to read the selected runners, projects, jobs, and logs.
- A chosen project/group/instance monitoring scope and polling settings.
- A Python, Docker, or Kubernetes runtime with persistent storage.
- An approved AI endpoint if AI diagnosis is desired.
- A Teams workflow webhook if Teams notifications are desired.

Credentials must be supplied privately through the deployment's configuration and secret handling, not through this document.

## 7. Example: from failure to recommendation

**Illustrative example, not a real incident:**

1. A build job fails with "no space left on device" in its log.
2. At a collection cycle, the agent records the failure and its runner, if known.
3. The AI reads the log excerpt and may inspect earlier output or failure history.
4. It suggests "Disk Full" as the category and recommends checking storage on the runner.
5. A Teams card presents the explanation, reported confidence, suggested action, and GitLab job link.
6. An engineer verifies the evidence and decides whether cleanup is appropriate.

The agent does not measure free disk space or perform cleanup in the current phases.

## 8. Important limitations and data handling

- **No automatic repairs:** the current service observes, diagnoses, and recommends.
- **No direct host telemetry:** disk utilization, Docker daemon health, CPU, and memory are not directly monitored by the implemented collectors.
- **AI can be wrong:** its confidence is self-reported, not an independently calibrated probability. Recommendations require human judgment.
- **Visibility is limited:** permissions, available logs, collection windows, and retained history affect what can be diagnosed.
- **Collection has a cost:** larger project scopes and more failures increase GitLab requests, AI requests, and processing time.
- **Data goes to configured services:** log excerpts, job metadata, and relevant history may be sent to the AI endpoint; operational summaries are sent to Teams.
- **Do not assume automatic secret removal:** logs can contain sensitive information. Use approved services, appropriate access controls, and safe logging practices.
- **Share the overview, not operational data:** this document contains no deployment credentials. Do not attach real logs, database exports, tokens, or webhook URLs to an external chatbot without authorization.

## 9. Phase 3: brief outlook

**Phase 3 is planned assisted remediation, not a current capability.**

The intended next step is to propose selected recovery actions, such as restarting a runner, cleaning Docker resources, or cleaning a workspace, and execute them only after human approval. The approval workflow, execution mechanism, and safeguards still need to be implemented.

Until then, engineers remain responsible for carrying out any recommended fix.

## 10. Frequently asked questions

**Is this a replacement for GitLab?**

No. GitLab still runs the pipelines and provides the source data. The agent adds monitoring, diagnosis, history, and notifications.

**Who is it for?**

DevOps, platform, SRE, and development teams investigating CI/CD failures.

**Is every failed job a runner issue?**

No. Failures may originate in application code, tests, dependencies, permissions, or external services.

**Does it fix failed builds automatically?**

No. Phases 1 and 2 recommend actions; engineers execute them.

**Does it need AI for basic monitoring?**

No. Runner monitoring, rule-based health scoring, failure recording, and basic alerts can work without AI diagnosis.

**Can it work without Teams?**

Yes. The service retains its records and API, while alerts are logged instead of delivered to Teams.

**Can it run with a local AI model?**

Yes, through the supported Ollama integration. That path uses supplied-context diagnosis rather than the current agentic tool-calling loop.

**Does a score of 100 guarantee the runner is healthy?**

No. It means the implemented scoring rules found no penalties in their available inputs, not that every aspect of the host was checked.

**Can it detect disk or Docker problems?**

It can suggest those causes from job-log evidence. Direct disk and Docker telemetry collection is deferred.

**Can it explain old failures again?**

Yes, for stored failures through re-diagnosis. The result depends on the evidence still available, including whether GitLab still has the log.

**Will a chatbot know our current runner status after reading this file?**

No. It can explain the project, but this file contains no live connection, current health data, or actual incident records.

## 11. Using this document with a chatbot

Upload this file by itself to an approved chatbot. No source code or credentials are needed for general project questions.

Suggested prompt:

> Use the attached Runner Health Agent overview to answer my questions in plain language. Distinguish implemented Phases 1 and 2 from planned Phase 3. Do not assume live system access or capabilities not described here. If the document does not provide an answer, say so.

Example questions:

- What is this project, and why is it useful?
- What happens after a GitLab job fails?
- How is health scoring different from AI diagnosis?
- What does "agentic" mean in this project?
- What alerts can a team receive?
- What can the agent not do yet?
- What is planned for Phase 3?
