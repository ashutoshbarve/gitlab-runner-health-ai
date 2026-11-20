import asyncio

import pytest

from app import ai_reasoning
from app.collectors.job_collector import _resolve_project_ids, rediagnose_failure
from app.config import Settings
from app.models import JobFailure


def test_job_failure_out_parses_stored_tool_calls_and_exposes_dependency_analysis():
    from app.schemas import JobFailureOut

    failure = JobFailure(
        id=1, project_id="1", job_name="j", stage="s", ref="r", failure_reason="missing_dependency_failure",
        web_url="", ai_root_cause="", ai_category="", ai_confidence=0, ai_recommended_action="", ai_error="",
        ai_tool_calls='["get_more_log_context"]', recurrence_count=0, dependency_analysis="needs 'setversion-job'",
    )

    out = JobFailureOut.model_validate(failure)

    assert out.ai_tool_calls == ["get_more_log_context"]
    assert out.dependency_analysis == "needs 'setversion-job'"


class _FakeGitLabClient:
    """Stands in for GitLabClient so tests don't hit the network."""

    def __init__(self, group_projects: dict[str, list[dict]]):
        self._group_projects = group_projects

    async def list_group_projects(self, http_client, group_id):
        return self._group_projects.get(group_id, [])


def _settings(**overrides) -> Settings:
    defaults = dict(gitlab_runner_scope="project", gitlab_project_ids="", gitlab_group_ids="")
    defaults.update(overrides)
    return Settings(**defaults)


def test_explicit_project_ids_take_priority_over_group_discovery():
    settings = _settings(gitlab_runner_scope="group", gitlab_project_ids="111,222", gitlab_group_ids="999")
    gitlab = _FakeGitLabClient({"999": [{"id": 333}]})

    result = asyncio.run(_resolve_project_ids(None, gitlab, settings))

    assert result == ["111", "222"]


def test_group_scope_auto_discovers_projects_when_no_project_ids():
    settings = _settings(gitlab_runner_scope="group", gitlab_group_ids="10,20")
    gitlab = _FakeGitLabClient({"10": [{"id": 1}, {"id": 2}], "20": [{"id": 3}]})

    result = asyncio.run(_resolve_project_ids(None, gitlab, settings))

    assert sorted(result) == ["1", "2", "3"]


def test_project_scope_without_project_ids_resolves_to_empty():
    settings = _settings(gitlab_runner_scope="project")
    gitlab = _FakeGitLabClient({})

    result = asyncio.run(_resolve_project_ids(None, gitlab, settings))

    assert result == []


# ---------------------------------------------------------------------------
# rediagnose_failure
# ---------------------------------------------------------------------------

class _FakeTraceResponse:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        pass


class _FakeHttpClient:
    """Stands in for httpx.AsyncClient so GitLabClient.get_job_trace() works
    without hitting the network."""

    def __init__(self, trace_text=None, raise_exc=None):
        self._trace_text = trace_text
        self._raise_exc = raise_exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def get(self, url, headers=None, timeout=None, params=None):
        if self._raise_exc:
            raise self._raise_exc
        return _FakeTraceResponse(self._trace_text)


@pytest.fixture
def db_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.database import Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.mark.asyncio
async def test_rediagnose_failure_refetches_log_and_updates_fields(db_session, monkeypatch):
    failure = JobFailure(
        id=1, project_id="100", job_name="build", stage="build", ref="main",
        failure_reason="script_failure", ai_error="Could not parse model output: ...",
    )
    db_session.add(failure)
    db_session.commit()

    async def fake_diagnose_agentic(settings, db, job_metadata, full_log, recurrence, runner_id, project_id):
        assert full_log == "fresh log content"
        return ai_reasoning.AIDiagnosis(
            root_cause="Fixed root cause", category="Disk Full", confidence=90,
            recommended_action="Clean up", tool_calls_made=[],
        )

    monkeypatch.setattr(ai_reasoning, "diagnose_agentic", fake_diagnose_agentic)
    settings = Settings(ai_reasoning_enabled=True)

    diagnosis = await rediagnose_failure(
        db_session, settings, failure, client_factory=lambda: _FakeHttpClient(trace_text="fresh log content")
    )

    assert diagnosis.succeeded
    assert failure.ai_category == "Disk Full"
    assert failure.ai_confidence == 90
    assert failure.ai_error == ""
    assert failure.log_excerpt == "fresh log content"


@pytest.mark.asyncio
async def test_rediagnose_failure_falls_back_to_stored_excerpt_if_refetch_fails(db_session, monkeypatch):
    import httpx

    failure = JobFailure(
        id=2, project_id="100", job_name="build", stage="build", ref="main",
        failure_reason="script_failure", log_excerpt="previously stored excerpt",
    )
    db_session.add(failure)
    db_session.commit()

    captured = {}

    async def fake_diagnose_agentic(settings, db, job_metadata, full_log, recurrence, runner_id, project_id):
        captured["full_log"] = full_log
        return ai_reasoning.AIDiagnosis(
            root_cause="x", category="Other", confidence=50, recommended_action="y", tool_calls_made=[]
        )

    monkeypatch.setattr(ai_reasoning, "diagnose_agentic", fake_diagnose_agentic)
    settings = Settings(ai_reasoning_enabled=True)

    await rediagnose_failure(
        db_session, settings, failure,
        client_factory=lambda: _FakeHttpClient(raise_exc=httpx.ConnectError("refused")),
    )

    assert captured["full_log"] == "previously stored excerpt"
