"""Thin async wrapper around the GitLab REST API (v4) endpoints we need."""
from datetime import datetime
from typing import Any

import httpx

from app.config import Settings


class GitLabClient:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._base_url = settings.gitlab_url.rstrip("/") + "/api/v4"
        self._headers = {"PRIVATE-TOKEN": settings.gitlab_token}

    async def _get(self, client: httpx.AsyncClient, path: str, params: dict[str, Any] | None = None) -> list[dict]:
        """GET a (possibly paginated) GitLab API list endpoint and return all results."""
        results: list[dict] = []
        url = f"{self._base_url}{path}"
        params = dict(params or {})
        params.setdefault("per_page", 100)
        page = 1
        while url:
            params["page"] = page
            resp = await client.get(url, headers=self._headers, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list):
                results.extend(data)
            next_page = resp.headers.get("x-next-page")
            if not next_page:
                break
            page = int(next_page)
        return results

    async def list_project_runners(self, client: httpx.AsyncClient, project_id: str) -> list[dict]:
        return await self._get(client, f"/projects/{project_id}/runners")

    async def list_group_runners(self, client: httpx.AsyncClient, group_id: str) -> list[dict]:
        return await self._get(client, f"/groups/{group_id}/runners")

    async def list_group_projects(self, client: httpx.AsyncClient, group_id: str) -> list[dict]:
        """List all non-archived projects under a group, including subgroups (for job-failure polling)."""
        return await self._get(
            client,
            f"/groups/{group_id}/projects",
            params={"include_subgroups": "true", "archived": "false", "simple": "true"},
        )

    async def list_instance_runners(self, client: httpx.AsyncClient) -> list[dict]:
        return await self._get(client, "/runners/all")

    async def list_failed_jobs(
        self, client: httpx.AsyncClient, project_id: str, since: datetime | None = None
    ) -> list[dict]:
        jobs = await self._get(client, f"/projects/{project_id}/jobs", params={"scope[]": "failed"})
        if since is None:
            return jobs
        filtered = []
        for job in jobs:
            finished_at = job.get("finished_at") or job.get("created_at")
            if not finished_at:
                continue
            ts = datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
            if ts >= since:
                filtered.append(job)
        return filtered

    async def _get_object(self, client: httpx.AsyncClient, path: str, params: dict[str, Any] | None = None) -> dict:
        resp = await client.get(f"{self._base_url}{path}", headers=self._headers, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    async def get_job(self, client: httpx.AsyncClient, project_id: str, job_id: int) -> dict:
        return await self._get_object(client, f"/projects/{project_id}/jobs/{job_id}")

    async def get_pipeline(self, client: httpx.AsyncClient, project_id: str, pipeline_id: int) -> dict:
        return await self._get_object(client, f"/projects/{project_id}/pipelines/{pipeline_id}")

    async def list_pipeline_jobs(self, client: httpx.AsyncClient, project_id: str, pipeline_id: int) -> list[dict]:
        """All jobs in a pipeline, including retried attempts."""
        return await self._get(
            client, f"/projects/{project_id}/pipelines/{pipeline_id}/jobs", params={"include_retried": "true"}
        )

    async def get_merged_ci_config(self, client: httpx.AsyncClient, project_id: str, sha: str) -> str:
        """The project's fully merged CI YAML (includes resolved) as of a commit."""
        data = await self._get_object(
            client, f"/projects/{project_id}/ci/lint", params={"content_ref": sha, "include_jobs": "true"}
        )
        return data.get("merged_yaml") or ""

    async def get_job_trace(self, client: httpx.AsyncClient, project_id: str, job_id: int) -> str:
        """Fetch a job's raw log (trace). Returns "" if unavailable (e.g. erased log)."""
        url = f"{self._base_url}/projects/{project_id}/jobs/{job_id}/trace"
        resp = await client.get(url, headers=self._headers, timeout=30)
        if resp.status_code == 404:
            return ""
        resp.raise_for_status()
        return resp.text
