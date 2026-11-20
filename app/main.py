"""Runner Health Agent - Phase 1 (Monitoring) FastAPI app."""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import failures, health, runners, trends
from app.config import get_settings
from app.database import init_db
from app.scheduler import start_scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

_scheduler = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    init_db()
    global _scheduler
    has_scope = settings.project_id_list or settings.group_id_list or settings.gitlab_runner_scope == "instance"
    if settings.gitlab_token and has_scope:
        _scheduler = start_scheduler(settings)
    else:
        logging.getLogger("runner_health_agent").warning(
            "GITLAB_TOKEN / GITLAB_PROJECT_IDS / GITLAB_GROUP_IDS not configured; scheduler not started. "
            "Set them in .env and restart."
        )
    yield
    if _scheduler:
        _scheduler.shutdown(wait=False)


app = FastAPI(
    title="Runner Health Agent",
    description="Phase 1 - Monitoring: GitLab runner availability + build failure tracking",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(runners.router)
app.include_router(failures.router)
app.include_router(health.router)
app.include_router(trends.router)


@app.get("/")
def root():
    return {"service": "runner-health-agent", "phase": "1-monitoring", "docs": "/docs"}
