"""Placeholder for Docker/disk telemetry collection.

GitLab's API does not expose per-runner disk or Docker daemon metrics, so
this requires one of:
  - a lightweight agent installed on each runner that pushes metrics to this
    service (e.g. POST /api/runners/{id}/metrics), or
  - a central SSH-based pull, or
  - scraping existing Prometheus node_exporter / cAdvisor endpoints if the
    runner fleet already exposes them.

This decision was deferred for the Phase 1 MVP (see Runner_Health_Agent_MVP.md,
Phase 1.5 / roadmap). Wire the chosen approach into this module and call it
from app.scheduler alongside the other collectors.
"""


async def poll_docker_disk_metrics() -> None:
    raise NotImplementedError(
        "Docker/disk metrics collection is not yet implemented - requires choosing an "
        "on-runner agent, SSH-pull, or Prometheus-scrape approach."
    )
