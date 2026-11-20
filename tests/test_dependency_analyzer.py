import pytest

from app.dependency_analyzer import analyze_missing_dependency, upstream_job_names

CI_YAML = """
stages: [setversion, review, test]
setversion-job:
  stage: setversion
  script: [echo v]
review_script:
  stage: review
  needs:
    - job: setversion-job
      artifacts: true
  script: [echo review]
unit-test-job:
  stage: test
  script: [echo test]
"""


def test_upstream_names_from_needs_respects_artifacts_flag():
    yaml_text = "build:\n  script: [x]\nrelease:\n  needs:\n    - job: build\n      artifacts: false\n    - lint\n  script: [x]\n"

    assert upstream_job_names(yaml_text, "release", "deploy") == ["lint"]


def test_upstream_names_from_dependencies():
    yaml_text = "a:\n  script: [x]\nb:\n  dependencies: [a]\n  script: [x]\n"

    assert upstream_job_names(yaml_text, "b", "test") == ["a"]


def test_upstream_names_default_to_all_earlier_stage_jobs():
    assert upstream_job_names(CI_YAML, "unit-test-job", "test") == ["setversion-job", "review_script"]


def test_upstream_names_none_when_job_unknown_or_yaml_invalid():
    assert upstream_job_names(CI_YAML, "nope", "test") is None
    assert upstream_job_names("a: [unclosed", "a", "test") is None


class _FakeGitLab:
    def __init__(self, upstream_jobs, ci_yaml=CI_YAML):
        self._upstream_jobs = upstream_jobs
        self._ci_yaml = ci_yaml

    async def get_job(self, client, project_id, job_id):
        return {
            "id": job_id, "name": "review_script", "stage": "review", "pipeline": {"id": 5},
            "created_at": "2026-11-19T05:04:26.920Z", "finished_at": "2026-11-19T05:04:28.477Z",
        }

    async def get_pipeline(self, client, project_id, pipeline_id):
        return {"sha": "abc", "created_at": "2026-10-21T06:42:33.493Z"}

    async def list_pipeline_jobs(self, client, project_id, pipeline_id):
        return self._upstream_jobs

    async def get_merged_ci_config(self, client, project_id, sha):
        return self._ci_yaml


def _setversion(**overrides):
    job = {
        "id": 100, "name": "setversion-job", "status": "success", "finished_at": "2026-10-21T06:43:20.529Z",
        "artifacts": [{"file_type": "trace"}], "artifacts_expire_at": "2026-10-23T06:43:18.835Z",
    }
    job.update(overrides)
    return job


@pytest.mark.asyncio
async def test_analysis_names_upstream_job_with_expired_artifacts_on_retry():
    result = await analyze_missing_dependency(_FakeGitLab([_setversion()]), None, "1", 200)

    assert "'setversion-job' (job #100)" in result
    assert "expired on 2026-10-23" in result
    assert "26 days before this job ran" in result
    assert "retried" in result


@pytest.mark.asyncio
async def test_analysis_reports_failed_upstream_job():
    result = await analyze_missing_dependency(
        _FakeGitLab([_setversion(status="failed")]), None, "1", 200
    )

    assert "has status 'failed'" in result


@pytest.mark.asyncio
async def test_analysis_uses_latest_retry_of_upstream_job():
    jobs = [_setversion(id=90, status="failed"), _setversion(id=100)]

    result = await analyze_missing_dependency(_FakeGitLab(jobs), None, "1", 200)

    assert "job #100" in result and "job #90" not in result


@pytest.mark.asyncio
async def test_analysis_without_ci_config_only_flags_jobs_that_cannot_supply_artifacts():
    jobs = [_setversion(), {"id": 101, "name": "unit-test-job", "status": "success", "artifacts": [{"file_type": "trace"}]}]

    result = await analyze_missing_dependency(_FakeGitLab(jobs, ci_yaml=""), None, "1", 200)

    assert "'setversion-job'" in result
    assert "unit-test-job" not in result
