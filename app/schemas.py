"""Pydantic response schemas for the API."""
import json
from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator


class RunnerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    description: str
    runner_type: str
    status: str
    is_paused: bool
    tag_list: str
    health_score: int
    issues: list[str]
    contacted_at: datetime | None
    updated_at: datetime

    @field_validator("issues", mode="before")
    @classmethod
    def _parse_issues(cls, v):
        if isinstance(v, str):
            return json.loads(v) if v else []
        return v


class JobFailureOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: str
    runner_id: int | None
    job_name: str
    stage: str
    ref: str
    failure_reason: str
    web_url: str
    created_at: datetime | None
    finished_at: datetime | None
    ai_root_cause: str
    ai_category: str
    ai_confidence: int
    ai_recommended_action: str
    ai_error: str
    ai_tool_calls: list[str]
    recurrence_count: int
    dependency_analysis: str

    @field_validator("ai_tool_calls", mode="before")
    @classmethod
    def _parse_tool_calls(cls, v):
        if isinstance(v, str):
            return json.loads(v) if v else []
        return v


class RecurringJobOut(BaseModel):
    project_id: str
    job_name: str
    failure_count: int
    last_failed_at: datetime | None


class HealthSummaryOut(BaseModel):
    total_runners: int
    online: int
    offline: int
    stale: int
    average_score: float
    runners_below_threshold: list[RunnerOut]
