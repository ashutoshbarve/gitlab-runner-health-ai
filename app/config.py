"""Application configuration, loaded from environment variables / .env file."""
from functools import lru_cache
from typing import List, Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # GitLab
    gitlab_url: str = "https://gitlab.com"
    gitlab_token: str = ""
    gitlab_project_ids: str = ""  # comma-separated; used directly, or to restrict job polling under a group
    gitlab_group_ids: str = ""  # comma-separated
    gitlab_runner_scope: Literal["project", "group", "instance"] = "project"

    # Polling / thresholds
    poll_interval_minutes: int = 5
    stale_threshold_minutes: int = 15
    failure_lookback_hours: int = 24
    health_score_alert_threshold: int = 70

    # Notifications
    teams_webhook_url: str = ""

    # Phase 2 - AI diagnostics
    ai_reasoning_enabled: bool = True
    ai_provider: Literal["openai_compatible", "ollama"] = "openai_compatible"
    # "openai_compatible": any /v1/chat/completions-style endpoint (e.g. an
    # internal model broker). ai_api_key is a secret - set via the K8s
    # Secret / .env, never a ConfigMap or committed file.
    ai_api_url: str = ""
    ai_api_key: str = ""
    ai_model: str = "gemma-27b-it-w4a16"
    # Comma-separated models tried in order if ai_model times out, is
    # unreachable, or returns an HTTP error (e.g. 503 "no healthy upstream").
    ai_fallback_models: str = "llama-3.3-70b,gpt-oss-120b"
    # "ollama": local/self-hosted Ollama instance, for offline dev use.
    ollama_url: str = "http://host.docker.internal:11434"
    ollama_model: str = "mistral:latest"
    ai_request_timeout_seconds: int = 60
    log_excerpt_max_chars: int = 4000
    # Agentic tool-calling: lets the model decide to fetch more log context,
    # runner failure history, or past diagnoses for this job before
    # answering, instead of always being handed a fixed context blob
    # up-front. Only takes effect for ai_provider=openai_compatible (not all
    # backends support tool calling); falls back to the single-shot path
    # otherwise.
    ai_agentic_tools_enabled: bool = True
    ai_agentic_max_tool_iterations: int = 4

    # Trend analysis
    trend_window_days: int = 7
    recurrence_alert_threshold: int = 3

    # Storage
    database_url: str = "sqlite:///./runner_health.db"

    @property
    def project_id_list(self) -> List[str]:
        return [p.strip() for p in self.gitlab_project_ids.split(",") if p.strip()]

    @property
    def ai_model_candidates(self) -> List[str]:
        models = [self.ai_model] + [m.strip() for m in self.ai_fallback_models.split(",") if m.strip()]
        return list(dict.fromkeys(models))

    @property
    def group_id_list(self) -> List[str]:
        return [g.strip() for g in self.gitlab_group_ids.split(",") if g.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
