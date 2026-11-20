import json

import httpx
import pytest

from app import ai_reasoning
from app.config import Settings
from app.trend_analyzer import RecurrenceInfo


def _settings(**overrides) -> Settings:
    defaults = dict(
        ai_reasoning_enabled=True,
        ai_provider="openai_compatible",
        ai_api_url="http://fake-broker/v1/chat/completions",
        ai_api_key="fake-key",
        ai_model="llama-3.3-70b",
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _recurrence():
    return RecurrenceInfo(same_job_failure_count=1, same_runner_failure_count=1, window_days=7)


def _job_metadata():
    return {"job_name": "x", "stage": "test", "ref": "main", "failure_reason": "y"}


class _FakeResponse:
    def __init__(self, json_data=None, raise_exc=None):
        self._json_data = json_data
        self._raise_exc = raise_exc

    def raise_for_status(self):
        if self._raise_exc:
            raise self._raise_exc

    def json(self):
        return self._json_data


class _FakeClient:
    """Mimics httpx.AsyncClient's async-context-manager + post() interface."""

    def __init__(self, response=None, responses=None, raise_exc=None):
        self._responses = list(responses) if responses is not None else ([response] if response else [])
        self._raise_exc = raise_exc
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append(json)
        if self._raise_exc:
            raise self._raise_exc
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _factory(client: _FakeClient):
    return lambda: client


def _chat_completion(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


def test_build_prompt_includes_job_metadata_and_log():
    prompt = ai_reasoning.build_prompt(_job_metadata(), "some error output", _recurrence())

    assert "x" in prompt  # job_name
    assert "some error output" in prompt
    assert "Disk Full" in prompt  # categories list present


def test_build_prompt_includes_dependency_analysis_only_when_present():
    metadata = {**_job_metadata(), "dependency_analysis": "setversion-job artifacts expired"}

    with_analysis = ai_reasoning.build_prompt(metadata, "", _recurrence())
    without = ai_reasoning.build_prompt(_job_metadata(), "", _recurrence())

    assert "setversion-job artifacts expired" in with_analysis
    assert "dependency analysis" not in without.lower()


@pytest.mark.asyncio
async def test_diagnose_parses_valid_openai_compatible_response():
    model_output = json.dumps(
        {
            "root_cause": "Disk ran out of space on the runner.",
            "category": "Disk Full",
            "confidence": 85,
            "recommended_action": "Clean up Docker images on the runner.",
        }
    )
    client = _FakeClient(response=_FakeResponse(json_data=_chat_completion(model_output)))
    settings = _settings()

    diagnosis = await ai_reasoning.diagnose(
        settings, job_metadata=_job_metadata(), log_excerpt="log", recurrence=_recurrence(),
        client_factory=_factory(client),
    )

    assert diagnosis.succeeded
    assert diagnosis.category == "Disk Full"
    assert diagnosis.confidence == 85
    assert "Disk ran out" in diagnosis.root_cause


@pytest.mark.asyncio
async def test_diagnose_strips_markdown_code_fences_before_parsing():
    model_output = "```json\n" + json.dumps(
        {"root_cause": "x", "category": "Docker Failure", "confidence": 70, "recommended_action": "y"}
    ) + "\n```"
    client = _FakeClient(response=_FakeResponse(json_data=_chat_completion(model_output)))
    settings = _settings()

    diagnosis = await ai_reasoning.diagnose(
        settings, job_metadata=_job_metadata(), log_excerpt="log", recurrence=_recurrence(),
        client_factory=_factory(client),
    )

    assert diagnosis.succeeded
    assert diagnosis.category == "Docker Failure"


@pytest.mark.asyncio
async def test_diagnose_repairs_unescaped_windows_path_backslashes():
    # Reproduces a real production failure: the model echoed a raw Windows
    # CI path into its JSON response without escaping the backslashes.
    model_output = (
        '{"root_cause": "Build failed in C:\\GitLab-Runner\\builds\\nbp9VFFwL\\0\\project.csproj", '
        '"category": "Script/Test Failure", "confidence": 80, "recommended_action": "Check the build log"}'
    )
    client = _FakeClient(response=_FakeResponse(json_data=_chat_completion(model_output)))
    settings = _settings()

    diagnosis = await ai_reasoning.diagnose(
        settings, job_metadata=_job_metadata(), log_excerpt="log", recurrence=_recurrence(),
        client_factory=_factory(client),
    )

    assert diagnosis.succeeded
    assert diagnosis.category == "Script/Test Failure"
    assert "C:\\GitLab-Runner" in diagnosis.root_cause


@pytest.mark.asyncio
async def test_diagnose_uses_ollama_backend_when_configured():
    model_output = json.dumps(
        {"root_cause": "x", "category": "Network Connectivity", "confidence": 60, "recommended_action": "y"}
    )
    client = _FakeClient(response=_FakeResponse(json_data={"response": model_output}))
    settings = _settings(ai_provider="ollama", ollama_url="http://fake-ollama:11434", ollama_model="mistral:latest")

    diagnosis = await ai_reasoning.diagnose(
        settings, job_metadata=_job_metadata(), log_excerpt="log", recurrence=_recurrence(),
        client_factory=_factory(client),
    )

    assert diagnosis.succeeded
    assert diagnosis.category == "Network Connectivity"


@pytest.mark.asyncio
async def test_diagnose_falls_back_on_malformed_json():
    client = _FakeClient(response=_FakeResponse(json_data=_chat_completion("not valid json")))
    settings = _settings()

    diagnosis = await ai_reasoning.diagnose(
        settings, job_metadata=_job_metadata(), log_excerpt="log", recurrence=_recurrence(),
        client_factory=_factory(client),
    )

    assert not diagnosis.succeeded
    assert diagnosis.category == "Other"
    assert diagnosis.confidence == 0


@pytest.mark.asyncio
async def test_diagnose_falls_back_on_http_error():
    client = _FakeClient(raise_exc=httpx.ConnectError("connection refused"))
    settings = _settings()

    diagnosis = await ai_reasoning.diagnose(
        settings, job_metadata=_job_metadata(), log_excerpt="log", recurrence=_recurrence(),
        client_factory=_factory(client),
    )

    assert not diagnosis.succeeded
    assert "connection refused" in diagnosis.error


@pytest.mark.asyncio
async def test_diagnose_error_names_exception_type_for_empty_message_timeouts():
    client = _FakeClient(raise_exc=httpx.ReadTimeout(""))
    settings = _settings(ai_fallback_models="")

    diagnosis = await ai_reasoning.diagnose(
        settings, job_metadata=_job_metadata(), log_excerpt="log", recurrence=_recurrence(),
        client_factory=_factory(client),
    )

    assert not diagnosis.succeeded
    assert "ReadTimeout" in diagnosis.error
    assert "llama-3.3-70b" in diagnosis.error


@pytest.mark.asyncio
async def test_diagnose_falls_back_to_next_model_on_timeout():
    model_output = json.dumps({"root_cause": "x", "category": "Other", "confidence": 60, "recommended_action": "y"})
    client = _FakeClient(responses=[httpx.ReadTimeout(""), _FakeResponse(json_data=_chat_completion(model_output))])
    settings = _settings(ai_model="primary", ai_fallback_models="secondary,primary")

    diagnosis = await ai_reasoning.diagnose(
        settings, job_metadata=_job_metadata(), log_excerpt="log", recurrence=_recurrence(),
        client_factory=_factory(client),
    )

    assert diagnosis.succeeded
    assert [c["model"] for c in client.calls] == ["primary", "secondary"]


@pytest.mark.asyncio
async def test_diagnose_agentic_falls_back_to_next_model_on_http_error(db_session):
    model_output = json.dumps({"root_cause": "x", "category": "Other", "confidence": 60, "recommended_action": "y"})
    client = _FakeClient(responses=[httpx.ReadTimeout(""), _final_answer_response(model_output)])
    settings = _settings(ai_model="primary", ai_fallback_models="secondary")

    diagnosis = await ai_reasoning.diagnose_agentic(
        settings, db=db_session, job_metadata=_job_metadata(), full_log="full log text", recurrence=_recurrence(),
        runner_id=1, project_id="100", client_factory=_factory(client),
    )

    assert diagnosis.succeeded
    assert [c["model"] for c in client.calls] == ["primary", "secondary"]


@pytest.mark.asyncio
async def test_diagnose_skips_network_call_when_disabled():
    client = _FakeClient(raise_exc=AssertionError("should not be called"))
    settings = _settings(ai_reasoning_enabled=False)

    diagnosis = await ai_reasoning.diagnose(
        settings, job_metadata=_job_metadata(), log_excerpt="log", recurrence=_recurrence(),
        client_factory=_factory(client),
    )

    assert not diagnosis.succeeded
    assert "disabled" in diagnosis.error.lower()


@pytest.mark.asyncio
async def test_diagnose_clamps_out_of_range_confidence():
    model_output = json.dumps(
        {"root_cause": "x", "category": "Other", "confidence": 500, "recommended_action": "y"}
    )
    client = _FakeClient(response=_FakeResponse(json_data=_chat_completion(model_output)))
    settings = _settings()

    diagnosis = await ai_reasoning.diagnose(
        settings, job_metadata=_job_metadata(), log_excerpt="log", recurrence=_recurrence(),
        client_factory=_factory(client),
    )

    assert diagnosis.confidence == 100


# ---------------------------------------------------------------------------
# Agentic tool-calling path
# ---------------------------------------------------------------------------

def _tool_call_response(name: str, arguments: dict) -> dict:
    return _FakeResponse(
        json_data={
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": name, "arguments": json.dumps(arguments)},
                            }
                        ],
                    }
                }
            ]
        }
    )


def _final_answer_response(content: str) -> dict:
    return _FakeResponse(json_data={"choices": [{"message": {"role": "assistant", "content": content}}]})


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
async def test_diagnose_agentic_answers_directly_without_tool_calls(db_session):
    model_output = json.dumps(
        {"root_cause": "x", "category": "Script/Test Failure", "confidence": 80, "recommended_action": "y"}
    )
    client = _FakeClient(responses=[_final_answer_response(model_output)])
    settings = _settings()

    diagnosis = await ai_reasoning.diagnose_agentic(
        settings, db=db_session, job_metadata=_job_metadata(), full_log="full log text", recurrence=_recurrence(),
        runner_id=1, project_id="100", client_factory=_factory(client),
    )

    assert diagnosis.succeeded
    assert diagnosis.category == "Script/Test Failure"
    assert diagnosis.tool_calls_made == []
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_diagnose_agentic_executes_requested_tool_then_answers(db_session):
    final_output = json.dumps(
        {"root_cause": "Runner-wide disk issue", "category": "Disk Full", "confidence": 90, "recommended_action": "y"}
    )
    client = _FakeClient(
        responses=[
            _tool_call_response("get_runner_failure_history", {}),
            _final_answer_response(final_output),
        ]
    )
    settings = _settings()

    diagnosis = await ai_reasoning.diagnose_agentic(
        settings, db=db_session, job_metadata=_job_metadata(), full_log="full log text", recurrence=_recurrence(),
        runner_id=1, project_id="100", client_factory=_factory(client),
    )

    assert diagnosis.succeeded
    assert diagnosis.category == "Disk Full"
    assert diagnosis.tool_calls_made == ["get_runner_failure_history"]
    assert len(client.calls) == 2
    # the tool result should have been appended as a "tool" role message for the second call
    second_call_messages = client.calls[1]["messages"]
    assert any(m.get("role") == "tool" for m in second_call_messages)


@pytest.mark.asyncio
async def test_diagnose_agentic_falls_back_after_max_iterations(db_session):
    settings = _settings(ai_agentic_max_tool_iterations=2)
    client = _FakeClient(
        responses=[
            _tool_call_response("get_runner_failure_history", {}),
            _tool_call_response("get_job_diagnosis_history", {}),
        ]
    )

    diagnosis = await ai_reasoning.diagnose_agentic(
        settings, db=db_session, job_metadata=_job_metadata(), full_log="full log text", recurrence=_recurrence(),
        runner_id=1, project_id="100", client_factory=_factory(client),
    )

    assert not diagnosis.succeeded
    assert "iteration limit" in diagnosis.error


@pytest.mark.asyncio
async def test_diagnose_agentic_falls_back_to_single_shot_for_ollama(db_session):
    model_output = json.dumps(
        {"root_cause": "x", "category": "Other", "confidence": 50, "recommended_action": "y"}
    )
    client = _FakeClient(response=_FakeResponse(json_data={"response": model_output}))
    settings = _settings(ai_provider="ollama", ollama_url="http://fake-ollama:11434", ollama_model="mistral:latest")

    diagnosis = await ai_reasoning.diagnose_agentic(
        settings, db=db_session, job_metadata=_job_metadata(), full_log="full log text", recurrence=_recurrence(),
        runner_id=1, project_id="100", client_factory=_factory(client),
    )

    assert diagnosis.succeeded
    # no tools param should have been sent on the single-shot ollama path
    assert "tools" not in client.calls[0]


@pytest.mark.asyncio
async def test_diagnose_agentic_falls_back_to_single_shot_when_tools_disabled(db_session):
    model_output = json.dumps(
        {"root_cause": "x", "category": "Other", "confidence": 50, "recommended_action": "y"}
    )
    client = _FakeClient(response=_FakeResponse(json_data=_chat_completion(model_output)))
    settings = _settings(ai_agentic_tools_enabled=False)

    diagnosis = await ai_reasoning.diagnose_agentic(
        settings, db=db_session, job_metadata=_job_metadata(), full_log="full log text", recurrence=_recurrence(),
        runner_id=1, project_id="100", client_factory=_factory(client),
    )

    assert diagnosis.succeeded
    assert "tools" not in client.calls[0]


def test_tool_get_more_log_context_returns_earlier_portion():
    ctx = ai_reasoning.ToolContext(
        db=None, full_log="A" * 100 + "B" * 50, runner_id=1, project_id="100", job_name="x", already_shown_chars=50
    )

    result = ai_reasoning._tool_get_more_log_context(ctx, max_chars=100)

    assert result["earlier_log_excerpt"] == "A" * 100


def test_tool_get_runner_failure_history_queries_same_runner(db_session):
    from app.models import JobFailure

    db_session.add(JobFailure(id=1, project_id="100", job_name="a", runner_id=7, failure_reason="script_failure"))
    db_session.add(JobFailure(id=2, project_id="100", job_name="b", runner_id=7, failure_reason="runner_system_failure"))
    db_session.add(JobFailure(id=3, project_id="100", job_name="c", runner_id=9, failure_reason="script_failure"))
    db_session.commit()

    ctx = ai_reasoning.ToolContext(db=db_session, full_log="", runner_id=7, project_id="100", job_name="a")
    result = ai_reasoning._tool_get_runner_failure_history(ctx)

    job_names = {r["job_name"] for r in result["recent_failures_on_runner"]}
    assert job_names == {"a", "b"}


def test_tool_get_job_diagnosis_history_filters_by_job_and_project(db_session):
    from app.models import JobFailure

    db_session.add(JobFailure(id=1, project_id="100", job_name="build", ai_category="Disk Full"))
    db_session.add(JobFailure(id=2, project_id="100", job_name="other-job", ai_category="Docker Failure"))
    db_session.add(JobFailure(id=3, project_id="200", job_name="build", ai_category="Network Connectivity"))
    db_session.commit()

    ctx = ai_reasoning.ToolContext(db=db_session, full_log="", runner_id=None, project_id="100", job_name="build")
    result = ai_reasoning._tool_get_job_diagnosis_history(ctx)

    categories = {r["category"] for r in result["past_diagnoses_for_this_job"]}
    assert categories == {"Disk Full"}
