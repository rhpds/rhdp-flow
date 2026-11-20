"""Durable background job manager for long-running operations.

Jobs are persisted in Postgres/SQLite (``jobs`` table) and kept in a
process-local cache for pause/cancel event objects. On process start,
non-terminal jobs are marked ``interrupted`` so callers can retry.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from sse_starlette import ServerSentEvent

from api.db import session_scope
from api.workspace_models import JobRow, Workspace
from api.workspace_store import DEFAULT_WORKSPACE, ensure_workspace, validate_workspace_id

logger = logging.getLogger("rhdp_flow.api")


def _make_set_event() -> asyncio.Event:
    e = asyncio.Event()
    e.set()
    return e


class Status(str, Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"
    paused = "paused"
    interrupted = "interrupted"


@dataclass
class Job:
    job_id: str
    workspace_id: str = DEFAULT_WORKSPACE
    status: Status = Status.pending
    progress: int = 0
    message: str = ""
    results: list[Any] | None = None
    error: str | None = None
    log_path: str | None = None
    created_at: float = field(default_factory=time.time)
    _events: asyncio.Queue = field(default_factory=asyncio.Queue, repr=False)
    _cancel_requested: bool = field(default=False, repr=False)
    _pause_event: asyncio.Event = field(default_factory=lambda: _make_set_event(), repr=False)
    # Event loop the job's async task + SSE queue live on (thread-safe progress).
    _loop: asyncio.AbstractEventLoop | None = field(default=None, repr=False)


MAX_JOBS = int(os.environ.get("RHDP_MAX_JOBS", "500"))
_jobs: dict[str, Job] = {}
_jobs_truncated: int = 0
_jobs_lock = threading.Lock()


def _persist(job: Job) -> None:
    ensure_workspace(job.workspace_id)
    with session_scope() as session:
        row = session.get(JobRow, job.job_id)
        if row is None:
            row = JobRow(job_id=job.job_id, workspace_id=job.workspace_id)
            session.add(row)
        row.workspace_id = job.workspace_id
        row.status = job.status.value
        row.progress = job.progress
        row.message = job.message or ""
        row.error = job.error
        row.results_json = json.dumps(job.results) if job.results is not None else None
        row.log_path = job.log_path
        row.cancel_requested = 1 if job._cancel_requested else 0
        row.updated_at = datetime.now(timezone.utc)


def _row_to_job(row: JobRow) -> Job:
    results = None
    if row.results_json:
        try:
            results = json.loads(row.results_json)
        except json.JSONDecodeError:
            results = None
    try:
        status = Status(row.status)
    except ValueError:
        status = Status.failed
    created = row.created_at.timestamp() if row.created_at else time.time()
    job = Job(
        job_id=row.job_id,
        workspace_id=row.workspace_id,
        status=status,
        progress=row.progress or 0,
        message=row.message or "",
        results=results,
        error=row.error,
        log_path=row.log_path,
        created_at=created,
    )
    job._cancel_requested = bool(row.cancel_requested)
    if status == Status.paused:
        job._pause_event.clear()
    return job


def interrupt_nonterminal_jobs() -> int:
    """On startup: mark pending/running/paused jobs as interrupted."""
    count = 0
    with session_scope() as session:
        rows = (
            session.query(JobRow)
            .filter(JobRow.status.in_(["pending", "running", "paused"]))
            .all()
        )
        for row in rows:
            row.status = Status.interrupted.value
            row.message = (row.message or "") + " (interrupted by process restart)"
            row.updated_at = datetime.now(timezone.utc)
            count += 1
    with _jobs_lock:
        _jobs.clear()
    if count:
        logger.warning("Marked %d in-flight job(s) as interrupted after restart", count)
    return count


def _cleanup_old_jobs() -> None:
    global _jobs_truncated
    with session_scope() as session:
        total = session.query(JobRow).count()
        if total <= MAX_JOBS:
            return
        terminal = (
            session.query(JobRow)
            .filter(JobRow.status.in_(["completed", "failed", "cancelled", "interrupted"]))
            .order_by(JobRow.created_at.asc())
            .limit(total - MAX_JOBS)
            .all()
        )
        for row in terminal:
            jid = row.job_id
            session.delete(row)
            with _jobs_lock:
                _jobs.pop(jid, None)
            _jobs_truncated += 1


def _put_nowait_safe(queue: asyncio.Queue, data: dict) -> None:
    """put_nowait that drops the event if the queue is full (SSE lag)."""
    try:
        queue.put_nowait(data)
    except asyncio.QueueFull:
        pass


def _pause_event_op(job: Job, op: str) -> None:
    """Set/clear a job's ``_pause_event`` from any thread, safely.

    ``request_cancel``/``request_pause``/``request_resume`` are called from the
    *sync* deploy control endpoints, which FastAPI runs in its threadpool — off
    the loop. ``asyncio.Event.set()`` wakes waiters via the non-threadsafe
    ``loop.call_soon`` (a paused deploy parks in ``wait_if_paused``), so calling
    it off-loop can lose the wakeup and wedge the loop. Marshal onto the job's
    loop, mirroring :func:`_emit_event`.
    """
    fn = job._pause_event.set if op == "set" else job._pause_event.clear
    loop = job._loop
    if loop is None or loop.is_closed():
        fn()
        return
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        fn()
    else:
        try:
            loop.call_soon_threadsafe(fn)
        except RuntimeError:
            pass


def _emit_event(job: Job, data: dict) -> None:
    """Push an SSE event onto the job's queue, thread-safely.

    ``update_job`` is called both from the event loop (endpoints) and from
    worker threads (``asyncio.to_thread`` deploy/QA runs). ``asyncio.Queue`` is
    not thread-safe, and its ``put_nowait`` wakes waiting getters via
    ``loop.call_soon`` — calling that off-loop corrupts the loop's ready queue
    and can silently wedge the loop (a completed ``to_thread`` never resumes its
    awaiting coroutine). Marshal off-thread writes back with
    ``call_soon_threadsafe``; write directly when already on the loop.
    """
    loop = job._loop
    if loop is None or loop.is_closed():
        # No bound loop (e.g. a unit test calling update_job directly).
        _put_nowait_safe(job._events, data)
        return
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        _put_nowait_safe(job._events, data)
    else:
        try:
            loop.call_soon_threadsafe(_put_nowait_safe, job._events, data)
        except RuntimeError:
            # Loop stopped/closed between the check and the call — nothing to
            # notify.
            pass



def create_job(workspace_id: str | None = None) -> Job:
    """Create a new pending job and return it."""
    _cleanup_old_jobs()
    wid = validate_workspace_id(workspace_id)
    ensure_workspace(wid)
    # Touch workspace row so FK exists
    with session_scope() as session:
        if session.get(Workspace, wid) is None:
            session.add(Workspace(id=wid))
    job_id = uuid.uuid4().hex[:12]
    job = Job(job_id=job_id, workspace_id=wid)
    try:
        job._loop = asyncio.get_running_loop()
    except RuntimeError:
        job._loop = None
    _persist(job)
    with _jobs_lock:
        _jobs[job_id] = job
    return job


def get_job(job_id: str) -> Job | None:
    """Return job by id, or None."""
    with _jobs_lock:
        cached = _jobs.get(job_id)
        if cached is not None:
            return cached
    with session_scope() as session:
        row = session.get(JobRow, job_id)
        if row is None:
            return None
        job = _row_to_job(row)
    with _jobs_lock:
        _jobs[job_id] = job
    return job


def get_stats() -> dict:
    """Return job store statistics including truncation info."""
    with session_scope() as session:
        total = session.query(JobRow).count()
    return {
        "total": total,
        "max": MAX_JOBS,
        "truncated": _jobs_truncated,
    }


def request_cancel(job_id: str) -> bool:
    """Request cancellation of a running job. Returns True if the job was found and running."""
    job = get_job(job_id)
    if job is None or job.status not in (Status.pending, Status.running, Status.paused):
        return False
    with _jobs_lock:
        job._cancel_requested = True
        _pause_event_op(job, "set")  # unpause if paused so loop can exit
    _persist(job)
    return True


def request_pause(job_id: str) -> bool:
    """Request pause of a running job."""
    job = get_job(job_id)
    if job is None or job.status != Status.running:
        return False
    with _jobs_lock:
        _pause_event_op(job, "clear")
        job.status = Status.paused
        _emit_event(job, _job_to_dict(job))
    _persist(job)
    return True


def request_resume(job_id: str) -> bool:
    """Resume a paused job."""
    job = get_job(job_id)
    if job is None or job.status != Status.paused:
        return False
    with _jobs_lock:
        _pause_event_op(job, "set")
        job.status = Status.running
        _emit_event(job, _job_to_dict(job))
    _persist(job)
    return True


def is_cancel_requested(job_id: str) -> bool:
    """Check if cancel has been requested for this job."""
    job = get_job(job_id)
    return job is not None and job._cancel_requested


async def wait_if_paused(job_id: str) -> None:
    """Block until the job is unpaused (or cancelled). Call between deploy steps."""
    job = get_job(job_id)
    if job is None:
        return
    await job._pause_event.wait()


def update_job(
    job_id: str,
    *,
    status: Status | None = None,
    progress: int | None = None,
    message: str | None = None,
    results: list[Any] | None = None,
    error: str | None = None,
    log_path: str | None = None,
) -> Job | None:
    """Update fields on an existing job. Pushes an SSE event."""
    job = get_job(job_id)
    if job is None:
        return None
    with _jobs_lock:
        if status is not None:
            job.status = status
        if progress is not None:
            job.progress = progress
        if message is not None:
            job.message = message
        if results is not None:
            job.results = results
        if error is not None:
            job.error = error
        if log_path is not None:
            job.log_path = log_path
        _emit_event(job, _job_to_dict(job))
    _persist(job)
    return job


async def event_generator(job_id: str):
    """Async generator yielding SSE events for a job."""
    from api.server import is_shutting_down

    job = get_job(job_id)
    if job is None:
        yield ServerSentEvent(data='{"error": "job not found"}', event="error")
        return
    yield ServerSentEvent(data=_serialize(job), event="status")
    while job.status in (Status.pending, Status.running, Status.paused):
        if is_shutting_down():
            yield ServerSentEvent(data='{"message": "server shutting down"}', event="closing")
            return
        try:
            data = await asyncio.wait_for(job._events.get(), timeout=30)
            yield ServerSentEvent(data=_serialize_dict(data), event="status")
            if data.get("status") in (
                Status.completed.value,
                Status.failed.value,
                Status.cancelled.value,
                Status.interrupted.value,
            ):
                return
        except TimeoutError:
            yield ServerSentEvent(data="", event="keepalive")
            job = get_job(job_id) or job
    yield ServerSentEvent(data=_serialize(job), event="status")


def _job_to_dict(job: Job) -> dict:
    return {
        "job_id": job.job_id,
        "workspace_id": job.workspace_id,
        "status": job.status.value,
        "progress": job.progress,
        "message": job.message,
        "error": job.error,
        "log_file": os.path.basename(job.log_path) if job.log_path else None,
        "cancel_requested": job._cancel_requested,
    }


def _serialize(job: Job) -> str:
    return json.dumps(_job_to_dict(job))


def _serialize_dict(d: dict) -> str:
    return json.dumps(d)
