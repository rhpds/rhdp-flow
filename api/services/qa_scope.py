"""QA floor-day scoping — match Labagator Ops Floor day pin.

Operators run Catalog → Setup → Healthy against one floor day (or Full event).
Floor day prefers optional CSV ``Session Date`` / ``Floor Date`` (Ops session_date);
falls back to the calendar day of ``Provisioning Date`` when unset.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime

# event-{id}-{DDMMYYYY}-{HHMM} — HHMM/date are event-local session wall clock.
# Workshop session block: event-42-24112026-1000
# Self-paced week project: event-42-sp (no local slot)
_EVENT_NS_SLOT_RE = re.compile(r"^event-\d+-(?:sp-)?(\d{8})-(\d{4})$")
_EVENT_SP_NS_RE = re.compile(r"^event-\d+-sp$")

# Labagator landing_export / ops_signals bands (event-local / CSV clock time).
TIME_BANDS: tuple[tuple[str, str, int, int], ...] = (
    ("morning", "Morning", 0, 12 * 60),
    ("midday", "Mid-day", 12 * 60, 15 * 60),
    ("afternoon", "Afternoon", 15 * 60, 24 * 60),
)

_CI_STAGES = frozenset(("event", "prod", "dev", "test"))


def _ci_base(ci_full: str) -> str:
    """Strip a known .stage suffix from a catalog item name.

    Labagator stores base names (e.g. rh1-2027.lb1063-foo) while Flow's CSV
    writes the full staged name (rh1-2027.lb1063-foo.test). Stripping lets
    either form match the other without requiring callers to agree on stage.
    """
    if "." in ci_full:
        last = ci_full.rsplit(".", 1)[-1]
        if last in _CI_STAGES:
            return ci_full[: -(len(last) + 1)]
    return ci_full

_BAND_BY_KEY = {key: (label, start, end) for key, label, start, end in TIME_BANDS}
VALID_TIME_BANDS = frozenset(_BAND_BY_KEY)
VALID_FLOOR = frozenset({"day", "event"})


def parse_provisioning_dt(value: str | None) -> datetime | None:
    """Parse Flow provisioning date (DD/MM/YYYY HH:MM) as a naive UTC clock."""
    if not value or not str(value).strip():
        return None
    raw = str(value).strip()
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%y %H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            dt = datetime.strptime(raw, fmt)
            if dt.tzinfo is not None:
                dt = dt.astimezone(UTC).replace(tzinfo=None)
            return dt
        except ValueError:
            continue
    return None


def normalize_floor_date(value: str | None) -> str | None:
    """Normalize YYYY-MM-DD / DD/MM/YYYY to YYYY-MM-DD."""
    if not value or not str(value).strip():
        return None
    raw = str(value).strip()[:10]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def date_key(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def time_band_key(dt: datetime) -> str:
    minutes = dt.hour * 60 + dt.minute
    for key, _label, start, end in TIME_BANDS:
        if start <= minutes < end:
            return key
    return "afternoon"


def band_label(key: str) -> str:
    info = _BAND_BY_KEY.get(key)
    return info[0] if info else key


def format_day_label(date_iso: str) -> str:
    """Human label like 'Wed 30 Sep' from YYYY-MM-DD (Ops Floor style)."""
    try:
        dt = datetime.strptime(date_iso, "%Y-%m-%d")
    except ValueError:
        return date_iso
    # Portable: avoid %-d (POSIX-only). Strip leading zero from day.
    return f"{dt.strftime('%a')} {dt.day} {dt.strftime('%b')}"


def schedule_floor_date(schedule) -> str | None:
    """Ops Floor day for a schedule row (session_date preferred)."""
    explicit = normalize_floor_date(getattr(schedule, "session_date", None) or "")
    if explicit:
        return explicit
    dt = parse_provisioning_dt(getattr(schedule, "provisioning_date", None))
    return date_key(dt) if dt else None


def normalize_ci_names(ci_names: Iterable[str] | None) -> frozenset[str] | None:
    """Return a frozenset of CI Name values, or None when the filter is unused."""
    if ci_names is None:
        return None
    cleaned = frozenset(str(n).strip() for n in ci_names if str(n).strip())
    return cleaned or None


def normalize_event_id(value: str | int | None) -> str | None:
    """Normalize Labagator event id for label / schedule matching."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def normalize_session_start(value: str | None) -> str | None:
    """Normalize session start to HHMM when possible."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) >= 4:
        return digits[:4]
    if ":" in raw:
        parts = raw.split(":")
        try:
            return f"{int(parts[0]):02d}{int(parts[1]):02d}"
        except (ValueError, IndexError):
            return raw
    return raw


def schedule_event_id(schedule) -> str | None:
    """Labagator event id stamped on the schedule row (CSV Event_Id / Workshop label)."""
    return normalize_event_id(getattr(schedule, "event_id", None) or "")


def schedule_session_start(schedule) -> str | None:
    """Session start as HHMM when present on the schedule row."""
    return normalize_session_start(getattr(schedule, "session_start", None) or "")


def is_selfpaced_event_namespace(namespace: str | None) -> bool:
    """True for Labagator self-paced projects (``event-{id}-sp``)."""
    ns = str(namespace or "").strip()
    return bool(_EVENT_SP_NS_RE.fullmatch(ns))


def parse_event_namespace_local_slot(namespace: str | None) -> tuple[str | None, str | None]:
    """Parse ``(YYYY-MM-DD, HHMM)`` event-local slot from an ``event-*`` namespace.

    Labagator encodes the Ops floor day + local session start in the project name
    (``event-42-24112026-1000`` → 2026-11-24 @ 10:00 local). CSV ``Session_Start``
    is UTC and must not be used for operator-facing labels.

    Self-paced week projects (``event-42-sp``) have no local slot.
    """
    ns = str(namespace or "").strip()
    if is_selfpaced_event_namespace(ns):
        return None, None
    match = _EVENT_NS_SLOT_RE.fullmatch(ns)
    if not match:
        return None, None
    ddmmyyyy, hhmm = match.group(1), match.group(2)
    day_s, month_s, year_s = ddmmyyyy[:2], ddmmyyyy[2:4], ddmmyyyy[4:]
    try:
        day, month, year = int(day_s), int(month_s), int(year_s)
        hour, minute = int(hhmm[:2]), int(hhmm[2:])
    except ValueError:
        return None, None
    if not (1 <= month <= 12 and 1 <= day <= 31 and 0 <= hour <= 23 and 0 <= minute <= 59):
        return None, None
    return f"{year:04d}-{month:02d}-{day:02d}", hhmm


def _slot_hhmm_from_event_namespace(namespace: str | None) -> str | None:
    """Parse HHMM from ``event-{id}-{DDMMYYYY}-{HHMM}`` namespaces."""
    _, hhmm = parse_event_namespace_local_slot(namespace)
    return hhmm


def _hhmm_as_datetime(hhmm: str | None) -> datetime | None:
    """Build a naive datetime whose clock equals HHMM (date unused)."""
    raw = normalize_session_start(hhmm)
    if not raw or len(raw) < 4 or not raw[:4].isdigit():
        return None
    try:
        hour, minute = int(raw[:2]), int(raw[2:4])
        if hour > 23 or minute > 59:
            return None
        return datetime(2000, 1, 1, hour, minute)
    except ValueError:
        return None


def schedule_matches_scope(
    schedule,
    *,
    floor: str | None = None,
    floor_date: str | None = None,
    time_band: str | None = None,
    ci_names: Iterable[str] | None = None,
    event_id: str | int | None = None,
    session_start: str | None = None,
) -> bool:
    """Return True if schedule row is in QA scope.

    ``floor=event`` / omitted → all rows (optionally still band-filtered).
    ``floor=day`` → only rows whose Ops floor date equals ``floor_date``.
    ``event_id`` → Labagator event label filter (preferred Ops path; no namespace required).
    ``session_start`` → optional HHMM filter for future session-scoped runs.
    ``ci_names`` → optional subset (retry-failed / pick which workshops to QA).
    """
    want_event = normalize_event_id(event_id)
    if want_event is not None:
        got_event = schedule_event_id(schedule)
        if got_event != want_event:
            return False

    want_start = normalize_session_start(session_start)
    if want_start is not None:
        got_start = schedule_session_start(schedule)
        if got_start != want_start:
            return False

    floor_mode = (floor or "event").strip().lower()
    if floor_mode not in VALID_FLOOR:
        floor_mode = "event"
    want_day = normalize_floor_date(floor_date) if floor_mode == "day" else None

    if want_day:
        got = schedule_floor_date(schedule)
        if got != want_day:
            return False

    if time_band:
        if time_band not in VALID_TIME_BANDS:
            return False
        dt = parse_provisioning_dt(getattr(schedule, "provisioning_date", None))
        if dt is None or time_band_key(dt) != time_band:
            return False

    want_cis = normalize_ci_names(ci_names)
    if want_cis is not None:
        # Match against all identities for the schedule row:
        #   ci_name  — human title from CSV ("LB1063: Defend, Contain and Comply")
        #   ci       — staged catalog slug ("rh1-2027.lb1063-defend-contain-comply.test")
        #   ci_base  — base slug without stage ("rh1-2027.lb1063-defend-contain-comply")
        # Labagator stores base names (no .stage); Flow's CSV writes the staged form.
        # Callers may send either — matching all three forms prevents stage-suffix mismatches.
        ci_full = str(getattr(schedule, "ci", None) or "").strip()
        candidates = {
            str(getattr(schedule, "ci_name", None) or "").strip(),
            ci_full,
            _ci_base(ci_full),
        }
        candidates.discard("")
        if not (candidates & want_cis):
            return False
    return True


def filter_schedules_by_scope(
    schedules: Iterable,
    *,
    namespace: str | None = None,
    floor: str | None = None,
    floor_date: str | None = None,
    time_band: str | None = None,
    ci_names: Iterable[str] | None = None,
    event_id: str | int | None = None,
    session_start: str | None = None,
) -> list:
    out = []
    want_cis = normalize_ci_names(ci_names)
    for s in schedules:
        if namespace and getattr(s, "namespace", None) != namespace:
            continue
        if not schedule_matches_scope(
            s,
            floor=floor,
            floor_date=floor_date,
            time_band=time_band,
            ci_names=want_cis,
            event_id=event_id,
            session_start=session_start,
        ):
            continue
        out.append(s)
    return out


def build_qa_scopes(schedules: Iterable, *, namespace: str | None = None) -> dict:
    """Derive Floor day / Full event options from loaded schedules."""
    scoped = [s for s in schedules if not namespace or getattr(s, "namespace", None) == namespace]
    by_day: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    unknown = 0
    for s in scoped:
        day = schedule_floor_date(s)
        if day is None:
            unknown += 1
            continue
        dt = parse_provisioning_dt(getattr(s, "provisioning_date", None))
        band = time_band_key(dt) if dt else "afternoon"
        by_day[day][band] += 1

    dates = []
    for day in sorted(by_day):
        bands = by_day[day]
        band_list = [
            {"key": key, "label": label, "count": bands.get(key, 0)}
            for key, label, _start, _end in TIME_BANDS
            if bands.get(key, 0) > 0
        ]
        dates.append(
            {
                "date": day,
                "label": format_day_label(day),
                "count": sum(bands.values()),
                "bands": band_list,
            }
        )

    return {
        "total": len(scoped),
        "unknown_date_count": unknown,
        "dates": dates,
    }


def _covered_key(entry: dict) -> tuple:
    """Stable identity for a covered row (dedupe across cumulative subset runs)."""
    return (
        (entry.get("namespace") or ""),
        (entry.get("ci_name") or ""),
        (entry.get("ci") or ""),
        (entry.get("session_date") or ""),
    )


def build_qa_coverage(
    schedules: Iterable,
    *,
    floor: str | None = None,
    floor_date: str | None = None,
    time_band: str | None = None,
    ci_names: Iterable[str] | None = None,
    namespaces: Iterable[str] | None = None,
    event_id: str | int | None = None,
    session_start: str | None = None,
) -> dict:
    """Describe exactly which schedule rows a QA run covered.

    Labagator consumes this so it can trust Flow's own coverage (what QA actually
    ran against) instead of reconstructing it from its session roster — the source
    of the "morning-only run shown as full-day" drift. The scope mirrors
    ``filter_schedules_by_scope`` and then narrows by namespace subset.
    """
    ns_filter: set[str] | None = None
    if namespaces:
        ns_filter = {str(n).strip() for n in namespaces if str(n).strip()} or None

    want_event = normalize_event_id(event_id)
    want_start = normalize_session_start(session_start)

    in_scope = filter_schedules_by_scope(
        schedules,
        floor=floor,
        floor_date=floor_date,
        time_band=time_band,
        ci_names=ci_names,
        event_id=want_event,
        session_start=want_start,
    )

    covered: list[dict] = []
    for s in in_scope:
        ns = getattr(s, "namespace", None)
        if ns_filter is not None and ns not in ns_filter:
            continue
        dt = parse_provisioning_dt(getattr(s, "provisioning_date", None))
        # Prefer HHMM embedded in event-* namespace (floor slot), then CSV
        # session_start, then provisioning clock. Provisioning is often UTC
        # evening and was greying Labagator morning/midday floor sessions.
        ss = _slot_hhmm_from_event_namespace(ns) or schedule_session_start(s)
        band_dt = _hhmm_as_datetime(ss) or dt
        covered.append(
            {
                "ci": getattr(s, "ci", None),
                "ci_name": getattr(s, "ci_name", None),
                "namespace": ns,
                "session_date": schedule_floor_date(s),
                "session_start": ss,
                "event_id": schedule_event_id(s),
                "time_band": time_band_key(band_dt) if band_dt else None,
            }
        )

    norm_cis = normalize_ci_names(ci_names)
    floor_mode = (floor or "event").strip().lower()
    if floor_mode not in VALID_FLOOR:
        floor_mode = "event"
    covered_namespaces = sorted({c["namespace"] for c in covered if c["namespace"]})

    return {
        "floor": floor_mode,
        "floor_date": normalize_floor_date(floor_date) if floor_date else None,
        "time_band": time_band if time_band in VALID_TIME_BANDS else None,
        "ci_names": sorted(norm_cis) if norm_cis else None,
        "namespaces": sorted(ns_filter) if ns_filter else covered_namespaces,
        "event_id": want_event,
        "session_start": want_start,
        "covered": covered,
        "expected_total": len(covered),
    }


def merge_qa_coverage(prior: dict | None, latest: dict) -> dict:
    """Fold a subset/retry run's coverage into the prior cumulative scope.

    Full runs (no ``ci_names``) replace the scope outright — they re-cover the
    whole floor/event. Subset runs union their ``covered`` rows into the prior
    scope by identity so earlier passes are not dropped, matching how
    ``/qa/results`` keeps prior passing rows on a targeted re-run.
    """
    if prior is None or not latest.get("ci_names"):
        return latest

    merged = dict(prior)
    by_key = {_covered_key(c): c for c in prior.get("covered", [])}
    for c in latest.get("covered", []):
        by_key[_covered_key(c)] = c
    merged["covered"] = list(by_key.values())
    merged["expected_total"] = len(merged["covered"])
    merged["namespaces"] = sorted(
        {c["namespace"] for c in merged["covered"] if c["namespace"]}
    ) or prior.get("namespaces")
    # The cumulative scope is no longer limited to one CI subset once broadened.
    prior_cis = prior.get("ci_names")
    if prior_cis:
        merged["ci_names"] = sorted(set(prior_cis) | set(latest.get("ci_names") or []))
    else:
        merged["ci_names"] = None
    return merged
