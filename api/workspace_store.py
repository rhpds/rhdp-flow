"""Durable, workspace-scoped Flow state (schedules / QA / results / archives).

Replaces process-global ``_schedules`` etc. Labagator uses ``event-{id}``;
standalone UI may use ``default`` or a user-created workspace id.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Header, HTTPException, Query

from api.db import session_scope
from api.secrets_crypto import decrypt_str, encrypt_str
from api.workspace_models import SessionArchive, Workspace, WorkspaceState

logger = logging.getLogger("rhdp_flow.api")

DEFAULT_WORKSPACE = "default"
WORKSPACE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
MAX_ARCHIVES_PER_WORKSPACE = 50

# Request-scoped workspace id (set by Depends / middleware helpers)
current_workspace_id: ContextVar[str] = ContextVar("current_workspace_id", default=DEFAULT_WORKSPACE)

_lock = threading.RLock()


@dataclass
class WorkspaceData:
    schedules: list[Any] = field(default_factory=list)
    deployment_results: list[Any] = field(default_factory=list)
    qa_results: list[Any] = field(default_factory=list)
    destroy_check_results: list[Any] = field(default_factory=list)
    asset_passwords: dict[str, str] | None = None
    current_filename: str = ""
    csv_text: str | None = None
    csv_filepath: str | None = None  # ephemeral tempfile path (not durable)
    deploy_log_path: str | None = None
    qa_log_path: str | None = None


def validate_workspace_id(raw: str | None) -> str:
    wid = (raw or DEFAULT_WORKSPACE).strip() or DEFAULT_WORKSPACE
    if not WORKSPACE_RE.match(wid):
        raise HTTPException(
            400,
            "Invalid workspace id (use 1-128 chars: letters, digits, _ . : -)",
        )
    return wid


def require_workspace(
    x_rhdp_workspace: str | None = Header(None, alias="X-RHDP-Workspace"),
    workspace: str | None = Query(None, description="Workspace id (Labagator: event-{id})"),
) -> str:
    """FastAPI dependency: resolve workspace from header or query."""
    wid = validate_workspace_id(x_rhdp_workspace or workspace)
    ensure_workspace(wid)
    current_workspace_id.set(wid)
    return wid


def ensure_workspace(workspace_id: str, owner: str | None = None) -> None:
    with _lock, session_scope() as session:
        row = session.get(Workspace, workspace_id)
        now = datetime.now(timezone.utc)
        if row is None:
            row = Workspace(id=workspace_id, owner=owner, created_at=now, updated_at=now, last_accessed_at=now)
            session.add(row)
            session.add(
                WorkspaceState(
                    workspace_id=workspace_id,
                    schedules_json="[]",
                    deployment_results_json="[]",
                    qa_results_json="[]",
                    destroy_check_results_json="[]",
                )
            )
        else:
            row.last_accessed_at = now
            if owner and not row.owner:
                row.owner = owner


def _ser_schedule(s: Any) -> dict:
    d = asdict(s) if hasattr(s, "__dataclass_fields__") else dict(s)
    if d.get("password"):
        d["password"] = encrypt_str(str(d["password"]))
    return d


def _de_schedule(d: dict, cls: type) -> Any:
    data = dict(d)
    if data.get("password"):
        try:
            data["password"] = decrypt_str(str(data["password"]))
        except Exception:  # noqa: BLE001
            pass
    # Drop unknown keys that WorkshopSchedule doesn't accept
    fields = getattr(cls, "__dataclass_fields__", {})
    filtered = {k: v for k, v in data.items() if k in fields}
    return cls(**filtered)


def _ser_result(r: Any) -> dict:
    d = asdict(r) if hasattr(r, "__dataclass_fields__") else dict(r)
    if d.get("password"):
        d["password"] = encrypt_str(str(d["password"]))
    return d


def _de_result(d: dict, cls: type) -> Any:
    data = dict(d)
    if data.get("password"):
        try:
            data["password"] = decrypt_str(str(data["password"]))
        except Exception:  # noqa: BLE001
            pass
    fields = getattr(cls, "__dataclass_fields__", {})
    filtered = {k: v for k, v in data.items() if k in fields}
    return cls(**filtered)


def _ser_qa(item: Any) -> Any:
    if hasattr(item, "model_dump"):
        return item.model_dump()
    if isinstance(item, dict):
        return item
    return asdict(item) if hasattr(item, "__dataclass_fields__") else item


def load(workspace_id: str | None = None) -> WorkspaceData:
    from rhdp_flow import DeploymentResult, WorkshopSchedule

    wid = validate_workspace_id(workspace_id or current_workspace_id.get())
    ensure_workspace(wid)
    with _lock, session_scope() as session:
        state = session.get(WorkspaceState, wid)
        if state is None:
            return WorkspaceData()
        schedules_raw = json.loads(state.schedules_json or "[]")
        results_raw = json.loads(state.deployment_results_json or "[]")
        qa_raw = json.loads(state.qa_results_json or "[]")
        destroy_raw = json.loads(state.destroy_check_results_json or "[]")
        pw_raw = json.loads(state.asset_passwords_json or "null")
        asset_passwords = None
        if isinstance(pw_raw, dict):
            asset_passwords = {}
            for k, v in pw_raw.items():
                try:
                    asset_passwords[k] = decrypt_str(str(v)) if v else ""
                except Exception:  # noqa: BLE001
                    asset_passwords[k] = str(v)
        return WorkspaceData(
            schedules=[_de_schedule(x, WorkshopSchedule) for x in schedules_raw if isinstance(x, dict)],
            deployment_results=[_de_result(x, DeploymentResult) for x in results_raw if isinstance(x, dict)],
            qa_results=qa_raw if isinstance(qa_raw, list) else [],
            destroy_check_results=destroy_raw if isinstance(destroy_raw, list) else [],
            asset_passwords=asset_passwords,
            current_filename=state.current_filename or "",
            csv_text=state.csv_text,
            deploy_log_path=state.deploy_log_path,
            qa_log_path=state.qa_log_path,
        )


def save(data: WorkspaceData, workspace_id: str | None = None) -> None:
    wid = validate_workspace_id(workspace_id or current_workspace_id.get())
    ensure_workspace(wid)
    schedules_json = json.dumps([_ser_schedule(s) for s in data.schedules])
    results_json = json.dumps([_ser_result(r) for r in data.deployment_results])
    qa_json = json.dumps([_ser_qa(q) for q in data.qa_results])
    destroy_json = json.dumps(data.destroy_check_results)
    pw_json = None
    if data.asset_passwords is not None:
        pw_json = json.dumps({k: encrypt_str(v) for k, v in data.asset_passwords.items()})
    with _lock, session_scope() as session:
        state = session.get(WorkspaceState, wid)
        if state is None:
            state = WorkspaceState(workspace_id=wid)
            session.add(state)
        state.schedules_json = schedules_json
        state.deployment_results_json = results_json
        state.qa_results_json = qa_json
        state.destroy_check_results_json = destroy_json
        state.asset_passwords_json = pw_json
        state.current_filename = data.current_filename or ""
        state.csv_text = data.csv_text
        state.deploy_log_path = data.deploy_log_path
        state.qa_log_path = data.qa_log_path
        state.updated_at = datetime.now(timezone.utc)
        ws = session.get(Workspace, wid)
        if ws:
            ws.updated_at = state.updated_at
            ws.last_accessed_at = state.updated_at


def update(mutator, workspace_id: str | None = None) -> WorkspaceData:
    """Load → mutate(callback) → save. ``mutator`` receives WorkspaceData and may modify in place."""
    wid = validate_workspace_id(workspace_id or current_workspace_id.get())
    with _lock:
        data = load(wid)
        mutator(data)
        save(data, wid)
        return data


def archive_current(workspace_id: str | None = None) -> str | None:
    """Archive current workspace state; returns session_id or None if empty."""
    from rhdp_flow import utc_timestamp_str

    wid = validate_workspace_id(workspace_id or current_workspace_id.get())
    data = load(wid)
    if not data.schedules and not data.deployment_results:
        return None
    with _lock, session_scope() as session:
        existing = (
            session.query(SessionArchive)
            .filter(SessionArchive.workspace_id == wid)
            .order_by(SessionArchive.id.desc())
            .all()
        )
        next_id = str(len(existing) + 1)
        payload = {
            "session_id": next_id,
            "filename": data.current_filename,
            "schedules": [_ser_schedule(s) for s in data.schedules],
            "deployment_results": [_ser_result(r) for r in data.deployment_results],
            "qa_results": [_ser_qa(q) for q in data.qa_results],
            "destroy_check_results": data.destroy_check_results,
            "schedule_count": len(data.schedules),
            "result_count": len(data.deployment_results),
            "timestamp": utc_timestamp_str(),
            "deploy_log_file": data.deploy_log_path.split("/")[-1] if data.deploy_log_path else None,
            "qa_log_file": data.qa_log_path.split("/")[-1] if data.qa_log_path else None,
        }
        session.add(
            SessionArchive(
                workspace_id=wid,
                session_id=next_id,
                payload_json=json.dumps(payload),
            )
        )
        # Trim old archives
        if len(existing) + 1 > MAX_ARCHIVES_PER_WORKSPACE:
            for old in existing[MAX_ARCHIVES_PER_WORKSPACE - 1 :]:
                session.delete(old)
        return next_id


def clear_workspace(workspace_id: str | None = None) -> int:
    """Archive then reset working state. Returns remaining archive count."""
    wid = validate_workspace_id(workspace_id or current_workspace_id.get())
    archive_current(wid)
    empty = WorkspaceData()
    save(empty, wid)
    with session_scope() as session:
        return (
            session.query(SessionArchive).filter(SessionArchive.workspace_id == wid).count()
        )


def list_archives(workspace_id: str | None = None) -> list[dict]:
    wid = validate_workspace_id(workspace_id or current_workspace_id.get())
    with session_scope() as session:
        rows = (
            session.query(SessionArchive)
            .filter(SessionArchive.workspace_id == wid)
            .order_by(SessionArchive.id.asc())
            .all()
        )
        out = []
        for r in rows:
            payload = json.loads(r.payload_json)
            out.append(
                {
                    "session_id": r.session_id,
                    "filename": payload.get("filename", ""),
                    "schedule_count": payload.get("schedule_count", 0),
                    "result_count": payload.get("result_count", 0),
                    "timestamp": payload.get("timestamp", ""),
                    "deploy_log_file": payload.get("deploy_log_file"),
                    "qa_log_file": payload.get("qa_log_file"),
                }
            )
        return out


def get_archive(session_id: str, workspace_id: str | None = None) -> dict | None:
    from rhdp_flow import DeploymentResult, WorkshopSchedule

    wid = validate_workspace_id(workspace_id or current_workspace_id.get())
    with session_scope() as session:
        row = (
            session.query(SessionArchive)
            .filter(SessionArchive.workspace_id == wid, SessionArchive.session_id == session_id)
            .first()
        )
        if not row:
            return None
        payload = json.loads(row.payload_json)
        # Decrypt schedules/results for restore into live state
        payload["schedules"] = [
            _de_schedule(x, WorkshopSchedule) for x in payload.get("schedules") or [] if isinstance(x, dict)
        ]
        payload["deployment_results"] = [
            _de_result(x, DeploymentResult) for x in payload.get("deployment_results") or [] if isinstance(x, dict)
        ]
        return payload


def list_workspaces() -> list[dict]:
    with session_scope() as session:
        rows = session.query(Workspace).order_by(Workspace.last_accessed_at.desc()).all()
        return [
            {
                "id": r.id,
                "owner": r.owner,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
                "last_accessed_at": r.last_accessed_at.isoformat() if r.last_accessed_at else None,
            }
            for r in rows
        ]


def purge_stale_workspaces(ttl_days: int = 90) -> int:
    """Delete standalone workspaces not accessed within ttl_days. Never deletes event-*."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=ttl_days)
    deleted = 0
    with session_scope() as session:
        rows = session.query(Workspace).all()
        for r in rows:
            if r.id.startswith("event-"):
                continue
            if r.id == DEFAULT_WORKSPACE:
                continue
            if r.last_accessed_at and r.last_accessed_at.replace(tzinfo=timezone.utc) < cutoff:
                session.delete(r)
                deleted += 1
    return deleted


# ---------------------------------------------------------------------------
# Compatibility façade used by routes.py — looks like the old module globals
# but reads/writes the ContextVar workspace.
# ---------------------------------------------------------------------------

class _WorkspaceStateProxy:
    """Attribute access maps to load/save of the current workspace."""

    def _get(self) -> WorkspaceData:
        return load(current_workspace_id.get())

    def _set_field(self, name: str, value: Any) -> None:
        def mut(data: WorkspaceData) -> None:
            setattr(data, name, value)

        update(mut, current_workspace_id.get())

    @property
    def schedules(self):
        return self._get().schedules

    @schedules.setter
    def schedules(self, value):
        self._set_field("schedules", list(value))

    @property
    def deployment_results(self):
        return self._get().deployment_results

    @deployment_results.setter
    def deployment_results(self, value):
        self._set_field("deployment_results", list(value))

    @property
    def qa_results(self):
        return self._get().qa_results

    @qa_results.setter
    def qa_results(self, value):
        self._set_field("qa_results", list(value))

    @property
    def destroy_check_results(self):
        return self._get().destroy_check_results

    @destroy_check_results.setter
    def destroy_check_results(self, value):
        self._set_field("destroy_check_results", list(value))

    @property
    def asset_passwords(self):
        return self._get().asset_passwords

    @asset_passwords.setter
    def asset_passwords(self, value):
        self._set_field("asset_passwords", value)

    @property
    def current_filename(self):
        return self._get().current_filename

    @current_filename.setter
    def current_filename(self, value):
        self._set_field("current_filename", value or "")

    @property
    def csv_filepath(self):
        return self._get().csv_filepath

    @csv_filepath.setter
    def csv_filepath(self, value):
        self._set_field("csv_filepath", value)

    @property
    def csv_text(self):
        return self._get().csv_text

    @csv_text.setter
    def csv_text(self, value):
        self._set_field("csv_text", value)

    @property
    def deploy_log_path(self):
        return self._get().deploy_log_path

    @deploy_log_path.setter
    def deploy_log_path(self, value):
        self._set_field("deploy_log_path", value)

    @property
    def qa_log_path(self):
        return self._get().qa_log_path

    @qa_log_path.setter
    def qa_log_path(self, value):
        self._set_field("qa_log_path", value)


# Singleton façade — routes assign to ``ws.schedules = …`` instead of globals
ws = _WorkspaceStateProxy()
