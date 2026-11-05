"""HTTP client for Labagator session/export endpoints.

Flow and Labagator run in the same OpenShift cluster. Flow calls
Labagator's in-cluster Service directly for read-only data (events list,
deploy-handoff CSV export) and for internal write-back operations that
use a shared API key instead of OAuth proxy auth (e.g. QA1 status push).
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, date, datetime, timedelta

import requests

logger = logging.getLogger(__name__)

LABAGATOR_BASE_URL = os.environ.get(
    "LABAGATOR_BASE_URL", "http://labagator-backend.labagator-dev.svc.cluster.local:8080"
)
_API_PREFIX = "/api/v1"
_TIMEOUT_SECONDS = 10


class LabagatorError(Exception):
    """Raised when Labagator is unreachable or returns an error response."""


def list_events(today: date | None = None, days: int | None = 7) -> list[dict]:
    """Return upcoming Labagator events, soonest first.

    ``days`` bounds how far ahead to look: an event is included when its date
    range overlaps ``[today, today + days]``. Pass ``days=None`` to return all
    upcoming events with no upper bound (still excludes events already ended).

    Asks Labagator for ``upcoming=true`` so we are not capped to the first 50
    events by primary key (which are often historical and filter to nothing).
    """
    today = today or datetime.now(tz=UTC).date()
    window_end = today + timedelta(days=days) if days is not None else None
    # Pull a wide upcoming page; Labagator filters end_date >= today server-side.
    params = {"upcoming": "true", "limit": 500}
    try:
        resp = requests.get(
            f"{LABAGATOR_BASE_URL}{_API_PREFIX}/events/",
            params=params,
            timeout=_TIMEOUT_SECONDS,
        )
    except requests.RequestException as e:
        raise LabagatorError(f"Labagator unreachable: {e}") from e
    if resp.status_code != 200:
        raise LabagatorError(f"Labagator returned {resp.status_code} listing events")

    events = resp.json()
    if not isinstance(events, list):
        raise LabagatorError("Labagator returned unexpected events payload")
    in_window = []
    for event in events:
        start = _parse_date(event["start_date"])
        end = _parse_date(event["end_date"])
        if end < today:
            continue
        if window_end is not None and start > window_end:
            continue
        in_window.append(event)
    in_window.sort(key=lambda e: e["start_date"])
    return in_window


def list_flow_sessions(event_id: int, filter_date: str | None = None) -> list[dict]:
    """Return one selectable summary per Flow-eligible room session for an event."""
    url = f"{LABAGATOR_BASE_URL}{_API_PREFIX}/exports/deploy-handoff/{event_id}/flow-sessions"
    params = {}
    if filter_date:
        params["filter_date"] = filter_date
    try:
        resp = requests.get(url, params=params, timeout=_TIMEOUT_SECONDS)
    except requests.RequestException as e:
        raise LabagatorError(f"Labagator unreachable: {e}") from e
    if resp.status_code != 200:
        raise LabagatorError(f"Labagator returned {resp.status_code}: {resp.text[:200]}")
    return resp.json().get("sessions", [])


def get_deploy_handoff_csv(
    event_id: int,
    namespace: str,
    enable_workshop_interface: bool,
    concurrency: int,
    white_glove: bool,
    auto_stop_days: int,
    auto_destroy_days: int,
    room_session_ids: list[int] | None = None,
) -> str:
    """Fetch the Flow-format CSV for one event's sessions from Labagator.

    When ``room_session_ids`` is provided, only those room sessions are exported
    (the session-picker path); otherwise the whole event is exported.
    """
    url = f"{LABAGATOR_BASE_URL}{_API_PREFIX}/exports/deploy-handoff/{event_id}/flow-csv"
    params = {
        "namespace": namespace,
        "enable_workshop_interface": enable_workshop_interface,
        "concurrency": concurrency,
        "white_glove": white_glove,
        "auto_stop_days": auto_stop_days,
        "auto_destroy_days": auto_destroy_days,
    }
    if room_session_ids:
        params["room_session_ids"] = ",".join(str(i) for i in room_session_ids)
    try:
        resp = requests.get(url, params=params, timeout=_TIMEOUT_SECONDS)
    except requests.RequestException as e:
        raise LabagatorError(f"Labagator unreachable: {e}") from e
    if resp.status_code != 200:
        raise LabagatorError(f"Labagator returned {resp.status_code}: {resp.text[:200]}")
    return resp.text


def push_qa1_status(
    *,
    namespace: str,
    floor_date: str,
    passed: int,
    failed: int,
    not_run: int,
    results: list[dict],
    run_at: str,
) -> None:
    """Push QA1 (catalog check) results to Labagator to patch the Confluence ops runbook.

    Requires ``LABAGATOR_INTERNAL_API_KEY`` env var to be set on both sides.
    Fails silently — QA results are primary; Confluence update is best-effort.
    """
    api_key = os.environ.get("LABAGATOR_INTERNAL_API_KEY", "").strip()
    if not api_key or not namespace or not floor_date:
        return
    try:
        resp = requests.post(
            f"{LABAGATOR_BASE_URL}{_API_PREFIX}/exports/internal/qa1-status",
            json={
                "namespace": namespace,
                "floor_date": floor_date,
                "passed": passed,
                "failed": failed,
                "not_run": not_run,
                "results": results,
                "run_at": run_at,
            },
            headers={"X-Internal-Api-Key": api_key},
            timeout=_TIMEOUT_SECONDS,
        )
        if resp.status_code not in (200, 201):
            logger.warning("QA1 status push to Labagator returned %s: %s", resp.status_code, resp.text[:200])
    except requests.RequestException as exc:
        logger.warning("QA1 status push to Labagator failed: %s", exc)


def _parse_date(value: str) -> date:
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(value).date()
