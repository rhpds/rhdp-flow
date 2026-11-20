"""Ensure Flow has schedules for Labagator label-scoped QA.

Ops QA buttons pass ``event_id`` (+ optional ``session_date``). Flow historically
required those rows to already be loaded via CSV / Send to Flow. When nothing
matches, auto-load from Labagator (preferred) or discover Workshops already on
the cluster by ``labagator.redhat.com/*`` labels.
"""

from __future__ import annotations

import csv
import io
import logging
import os
import subprocess
import tempfile
from datetime import datetime
from typing import Any, Callable

from api.services import labagator_client
from api.services.qa_scope import (
    filter_schedules_by_scope,
    normalize_event_id,
    normalize_floor_date,
    normalize_session_start,
    schedule_event_id,
)
from rhdp_flow import (
    LABAGATOR_EVENT_ID_LABEL,
    LABAGATOR_SESSION_DATE_LABEL,
    LABAGATOR_SESSION_START_LABEL,
    WorkshopSchedule,
    read_csv_input,
)

logger = logging.getLogger("rhdp_flow.qa_schedule_resolve")


def qa_scope_error_message(
    *,
    event_id: str | None = None,
    session_date: str | None = None,
    session_start: str | None = None,
) -> str:
    """Operator-facing hint when label scope still has no schedules."""
    bits: list[str] = []
    if event_id:
        bits.append(f"event_id={event_id}")
    if session_date:
        bits.append(f"session_date={session_date}")
    if session_start:
        bits.append(f"session_start={session_start}")
    scope = " · ".join(bits) if bits else "the requested scope"
    return (
        f"No schedules match {scope}. "
        "In Labagator use Deploy → Send to Flow for this event/day "
        "(stamps Event_Id + Session Date), or import the event CSV in Flow, then re-run QA."
    )


def parse_schedules_from_csv_text(csv_text: str) -> list[WorkshopSchedule]:
    """Parse Flow-format CSV text into WorkshopSchedule rows."""
    text = (csv_text or "").lstrip("\ufeff")
    if not text.strip():
        return []
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, encoding="utf-8")
    try:
        tmp.write(text)
        tmp.close()
        return list(read_csv_input(tmp.name))
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def fetch_labagator_schedules(
    event_id: str,
    *,
    session_date: str | None = None,
    namespace: str = "",
) -> list[WorkshopSchedule]:
    """Pull Flow-format CSV from Labagator for an event (optional day filter)."""
    eid = normalize_event_id(event_id)
    if not eid:
        return []
    try:
        event_int = int(eid)
    except ValueError:
        logger.warning("QA auto-load: non-numeric event_id=%r", eid)
        return []

    filter_date = normalize_floor_date(session_date) if session_date else None
    try:
        csv_text = labagator_client.get_deploy_handoff_csv(
            event_id=event_int,
            namespace=namespace or "",
            enable_workshop_interface=True,
            concurrency=10,
            white_glove=True,
            auto_stop_days=0,
            auto_destroy_days=0,
            filter_date=filter_date,
        )
    except labagator_client.LabagatorError as exc:
        logger.info("QA auto-load from Labagator failed for event %s: %s", eid, exc)
        return []

    try:
        schedules = parse_schedules_from_csv_text(csv_text)
    except ValueError as exc:
        logger.warning("QA auto-load: Labagator CSV parse failed for event %s: %s", eid, exc)
        return []
    logger.info(
        "QA auto-load: imported %d schedule row(s) from Labagator for event %s%s",
        len(schedules),
        eid,
        f" day {filter_date}" if filter_date else "",
    )
    return schedules


def _iso_to_flow_datetime(value: str) -> str:
    """Convert ISO / RFC3339 timestamps to Flow CSV ``DD/MM/YYYY HH:MM`` when possible."""
    raw = (value or "").strip()
    if not raw:
        return ""
    cleaned = raw.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        return dt.strftime("%d/%m/%Y %H:%M")
    except ValueError:
        return raw


def workshop_cr_to_schedule(ws: dict[str, Any]) -> WorkshopSchedule | None:
    """Build a minimal WorkshopSchedule from a Workshop CR for QA CSV scoping."""
    meta = ws.get("metadata") or {}
    spec = ws.get("spec") or {}
    labels = meta.get("labels") or {}
    annotations = meta.get("annotations") or {}
    namespace = str(meta.get("namespace") or "").strip()
    if not namespace:
        return None

    ci = str(labels.get("babylon.gpte.redhat.com/catalogItemName") or "").strip()
    ci_name = str(
        annotations.get("babylon.gpte.redhat.com/catalogItemDisplayName")
        or spec.get("displayName")
        or ci
        or meta.get("name")
        or ""
    ).strip()
    if not ci and not ci_name:
        return None

    catalog_ns = str(labels.get("babylon.gpte.redhat.com/catalogItemNamespace") or "").strip()
    action = spec.get("actionSchedule") or {}
    lifespan = spec.get("lifespan") or {}
    has_ui = bool((spec.get("labUserInterface") or {}).get("redirect", False))

    return WorkshopSchedule(
        ci_name=ci_name or ci,
        ci=ci or ci_name,
        namespace=namespace,
        enable_workshop_interface=has_ui,
        password=str(spec.get("accessPassword") or ""),
        activity=str(annotations.get("demo.redhat.com/purpose-activity") or "Admin"),
        purpose=str(annotations.get("demo.redhat.com/purpose") or "QA"),
        workshop_name=str(spec.get("displayName") or ci_name or ci),
        provisioning_date=_iso_to_flow_datetime(str(action.get("start") or "")),
        auto_stop=_iso_to_flow_datetime(str(action.get("stop") or "")),
        auto_destroy=_iso_to_flow_datetime(str(lifespan.get("end") or "")),
        session_date=str(labels.get(LABAGATOR_SESSION_DATE_LABEL) or "").strip()[:10],
        session_start=str(labels.get(LABAGATOR_SESSION_START_LABEL) or "").strip(),
        event_id=str(labels.get(LABAGATOR_EVENT_ID_LABEL) or "").strip(),
        catalog_namespace=catalog_ns,
        redirect=has_ui,
    )


def discover_schedules_by_labagator_labels(
    config: Any,
    event_id: str,
    *,
    session_date: str | None = None,
) -> list[WorkshopSchedule]:
    """``oc get workshop -A -l labagator.redhat.com/event-id=…`` → schedules."""
    eid = normalize_event_id(event_id)
    if not eid or config is None:
        return []

    selector = f"{LABAGATOR_EVENT_ID_LABEL}={eid}"
    day = normalize_floor_date(session_date) if session_date else None
    if day:
        selector = f"{selector},{LABAGATOR_SESSION_DATE_LABEL}={day}"

    env = os.environ.copy()
    kc = getattr(config, "kubeconfig_path", None)
    if kc:
        env["KUBECONFIG"] = kc
    oc = getattr(config, "oc_command", None) or "oc"
    timeout = getattr(config, "timeout", 60) or 60

    cmd = [oc, "get", "workshop", "-A", "-l", selector, "-o", "json"]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, env=env, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.info("QA workshop discovery failed for %s: %s", selector, exc)
        return []

    if result.returncode != 0:
        logger.info(
            "QA workshop discovery oc failed (%s): %s",
            result.returncode,
            (result.stderr or "")[:300],
        )
        return []

    try:
        import json

        items = json.loads(result.stdout or "{}").get("items") or []
    except (TypeError, ValueError) as exc:
        logger.warning("QA workshop discovery: invalid JSON: %s", exc)
        return []

    out: list[WorkshopSchedule] = []
    for ws in items:
        sched = workshop_cr_to_schedule(ws)
        if sched is not None:
            out.append(sched)
    logger.info(
        "QA workshop discovery: %d Workshop(s) for selector %s",
        len(out),
        selector,
    )
    return out


def merge_schedules_for_event(
    existing: list[WorkshopSchedule],
    incoming: list[WorkshopSchedule],
    event_id: str,
) -> list[WorkshopSchedule]:
    """Replace rows for ``event_id`` with ``incoming``; keep other events' rows."""
    want = normalize_event_id(event_id)
    if not want:
        return list(existing) + list(incoming)
    kept = [s for s in existing if schedule_event_id(s) != want]
    return kept + list(incoming)


def ensure_label_scoped_schedules(
    schedules: list[WorkshopSchedule],
    *,
    event_id: str | None,
    floor: str | None = None,
    floor_date: str | None = None,
    time_band: str | None = None,
    ci_names: list[str] | None = None,
    session_start: str | None = None,
    get_config: Callable[[], Any] | None = None,
) -> tuple[list[WorkshopSchedule], list[WorkshopSchedule]]:
    """Return ``(updated_all_schedules, label_scoped)``.

    When the requested Labagator label scope matches nothing, try Labagator CSV
    auto-load, then Workshop CR discovery. Mutates are returned as a new full
    schedule list for the caller to install into Flow's process state.
    """
    want_event = normalize_event_id(event_id)
    want_day = normalize_floor_date(floor_date) if (floor or "").strip().lower() == "day" else None
    # session_date label filter uses floor_date when Ops pinned a day
    session_date_for_fetch = want_day or normalize_floor_date(floor_date)

    def _scoped(rows: list[WorkshopSchedule]) -> list[WorkshopSchedule]:
        return filter_schedules_by_scope(
            rows,
            floor=floor,
            floor_date=floor_date,
            time_band=time_band,
            ci_names=ci_names,
            event_id=want_event,
            session_start=normalize_session_start(session_start),
        )

    current = list(schedules)
    scoped = _scoped(current)
    if scoped or not want_event:
        return current, scoped

    # 1) Labagator CSV (stamps Event_Id + Session Date)
    imported = fetch_labagator_schedules(
        want_event,
        session_date=session_date_for_fetch,
    )
    if imported:
        current = merge_schedules_for_event(current, imported, want_event)
        scoped = _scoped(current)
        if scoped:
            return current, scoped

    # 2) Cluster Workshops already labeled for this event/day
    if get_config is not None:
        try:
            config = get_config()
        except Exception as exc:  # noqa: BLE001 — discovery is best-effort
            logger.info("QA workshop discovery: config unavailable: %s", exc)
            config = None
        if config is not None:
            discovered = discover_schedules_by_labagator_labels(
                config,
                want_event,
                session_date=session_date_for_fetch,
            )
            if discovered:
                current = merge_schedules_for_event(current, discovered, want_event)
                scoped = _scoped(current)

    return current, scoped


def schedules_to_minimal_csv(schedules: list[WorkshopSchedule]) -> str:
    """Serialize schedules for debugging / temp QA CSVs (not used by routes yet)."""
    fieldnames = [
        "CI Name",
        "CI",
        "Namespace",
        "Users",
        "Enable_workshop_interface",
        "Password",
        "Activity",
        "Purpose",
        "Workshop Name",
        "Provisioning Date (UTC)",
        "Auto-stop (UTC)",
        "Auto-destroy (UTC)",
        "Session Date",
        "Session_Start",
        "Event_Id",
    ]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    for s in schedules:
        writer.writerow(
            {
                "CI Name": s.ci_name,
                "CI": s.ci,
                "Namespace": s.namespace,
                "Users": "" if s.users is None else s.users,
                "Enable_workshop_interface": s.enable_workshop_interface,
                "Password": s.password,
                "Activity": s.activity,
                "Purpose": s.purpose,
                "Workshop Name": s.workshop_name,
                "Provisioning Date (UTC)": s.provisioning_date,
                "Auto-stop (UTC)": s.auto_stop,
                "Auto-destroy (UTC)": s.auto_destroy,
                "Session Date": getattr(s, "session_date", "") or "",
                "Session_Start": getattr(s, "session_start", "") or "",
                "Event_Id": getattr(s, "event_id", "") or "",
            }
        )
    return buf.getvalue()
