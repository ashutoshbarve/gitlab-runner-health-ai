"""Database models for Runner Health Agent - Phase 1 (Monitoring)."""
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Runner(Base):
    __tablename__ = "runners"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # GitLab runner id
    description: Mapped[str] = mapped_column(String(255), default="")
    runner_type: Mapped[str] = mapped_column(String(50), default="")
    status: Mapped[str] = mapped_column(String(30), default="unknown")
    # online/offline/stale/never_contacted (mirrors GitLab's runner "status" field)
    is_paused: Mapped[bool] = mapped_column(Boolean, default=False)
    tag_list: Mapped[str] = mapped_column(String(500), default="")
    ip_address: Mapped[str] = mapped_column(String(100), default="")
    contacted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    health_score: Mapped[int] = mapped_column(Integer, default=100)
    issues: Mapped[str] = mapped_column(Text, default="")  # JSON-encoded list of current issues
    score_alert_active: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    status_events: Mapped[list["RunnerStatusEvent"]] = relationship(
        back_populates="runner", cascade="all, delete-orphan"
    )


class RunnerStatusEvent(Base):
    """History of status transitions, used both for an audit trail and to avoid re-alerting."""

    __tablename__ = "runner_status_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    runner_id: Mapped[int] = mapped_column(ForeignKey("runners.id"))
    previous_status: Mapped[str] = mapped_column(String(30), default="")
    new_status: Mapped[str] = mapped_column(String(30), default="")
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    notified: Mapped[bool] = mapped_column(Boolean, default=False)

    runner: Mapped["Runner"] = relationship(back_populates="status_events")


class JobFailure(Base):
    """A failed GitLab CI job, optionally correlated to the runner that executed it."""

    __tablename__ = "job_failures"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # GitLab job id
    project_id: Mapped[str] = mapped_column(String(50))
    runner_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    job_name: Mapped[str] = mapped_column(String(255), default="")
    stage: Mapped[str] = mapped_column(String(100), default="")
    ref: Mapped[str] = mapped_column(String(255), default="")
    failure_reason: Mapped[str] = mapped_column(String(100), default="")
    web_url: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notified: Mapped[bool] = mapped_column(Boolean, default=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    # Phase 2 - AI diagnostics
    log_excerpt: Mapped[str] = mapped_column(Text, default="")
    ai_root_cause: Mapped[str] = mapped_column(Text, default="")
    ai_category: Mapped[str] = mapped_column(String(50), default="")
    ai_confidence: Mapped[int] = mapped_column(Integer, default=0)
    ai_recommended_action: Mapped[str] = mapped_column(Text, default="")
    ai_error: Mapped[str] = mapped_column(Text, default="")
    ai_tool_calls: Mapped[str] = mapped_column(Text, default="")  # JSON list of tool names the model invoked
    recurrence_count: Mapped[int] = mapped_column(Integer, default=0)
    # Populated only for GitLab missing_dependency_failure jobs: which upstream job(s) the job needs and why they're unavailable
    dependency_analysis: Mapped[str] = mapped_column(Text, default="")


class AlertLog(Base):
    """Record of every alert sent (or attempted), for audit/troubleshooting."""

    __tablename__ = "alert_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    alert_type: Mapped[str] = mapped_column(String(50))
    subject: Mapped[str] = mapped_column(String(255))
    payload: Mapped[str] = mapped_column(Text)
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
