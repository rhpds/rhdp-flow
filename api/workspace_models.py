"""ORM models for durable multi-workspace Flow state."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from api.db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
    last_accessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    # Optional owner hint (Labagator email / service account); not ACL yet
    owner: Mapped[str | None] = mapped_column(String(256), nullable=True)

    state: Mapped[WorkspaceState | None] = relationship(
        back_populates="workspace", uselist=False, cascade="all, delete-orphan"
    )
    archives: Mapped[list[SessionArchive]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan"
    )
    jobs: Mapped[list[JobRow]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan"
    )


class WorkspaceState(Base):
    __tablename__ = "workspace_state"

    workspace_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    schedules_json: Mapped[str] = mapped_column(Text, default="[]")
    deployment_results_json: Mapped[str] = mapped_column(Text, default="[]")
    qa_results_json: Mapped[str] = mapped_column(Text, default="[]")
    destroy_check_results_json: Mapped[str] = mapped_column(Text, default="[]")
    asset_passwords_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_filename: Mapped[str] = mapped_column(String(512), default="")
    csv_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    deploy_log_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    qa_log_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    workspace: Mapped[Workspace] = relationship(back_populates="state")


class SessionArchive(Base):
    __tablename__ = "session_archives"
    __table_args__ = (UniqueConstraint("workspace_id", "session_id", name="uq_ws_session"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    session_id: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    workspace: Mapped[Workspace] = relationship(back_populates="archives")


class JobRow(Base):
    __tablename__ = "jobs"

    job_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    results_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    log_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    cancel_requested: Mapped[int] = mapped_column(Integer, default=0)  # 0/1
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    workspace: Mapped[Workspace] = relationship(back_populates="jobs")
