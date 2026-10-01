"""Floor-day QA scoping (Ops Floor pin)."""

from types import SimpleNamespace

from api.services.qa_scope import (
    build_qa_coverage,
    build_qa_scopes,
    filter_schedules_by_scope,
    format_day_label,
    merge_qa_coverage,
    schedule_floor_date,
)


def _row(**kwargs):
    defaults = dict(
        ci_name="W",
        ci="w.prod",
        namespace="ns",
        provisioning_date="30/09/2026 10:30",
        session_date="",
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_session_date_preferred_over_provisioning_day():
    s = _row(session_date="2026-09-30", provisioning_date="29/09/2026 22:00")
    assert schedule_floor_date(s) == "2026-09-30"


def test_falls_back_to_provisioning_calendar_day():
    s = _row(session_date="", provisioning_date="01/10/2026 13:00")
    assert schedule_floor_date(s) == "2026-10-01"


def test_filter_floor_day_excludes_other_days():
    rows = [
        _row(ci_name="Wed", session_date="2026-09-30", provisioning_date="30/09/2026 10:00"),
        _row(ci_name="Thu", session_date="2026-10-01", provisioning_date="01/10/2026 10:00"),
    ]
    scoped = filter_schedules_by_scope(rows, floor="day", floor_date="2026-09-30")
    assert [r.ci_name for r in scoped] == ["Wed"]


def test_filter_full_event_keeps_all():
    rows = [
        _row(session_date="2026-09-30"),
        _row(session_date="2026-10-01", provisioning_date="01/10/2026 15:30"),
    ]
    scoped = filter_schedules_by_scope(rows, floor="event")
    assert len(scoped) == 2


def test_time_band_midday():
    rows = [
        _row(ci_name="AM", provisioning_date="30/09/2026 10:30", session_date="2026-09-30"),
        _row(ci_name="MID", provisioning_date="30/09/2026 13:00", session_date="2026-09-30"),
        _row(ci_name="PM", provisioning_date="30/09/2026 15:30", session_date="2026-09-30"),
    ]
    scoped = filter_schedules_by_scope(
        rows, floor="day", floor_date="2026-09-30", time_band="midday"
    )
    assert [r.ci_name for r in scoped] == ["MID"]


def test_filter_ci_names_subset():
    rows = [
        _row(ci_name="Keep", session_date="2026-09-30"),
        _row(ci_name="Skip", session_date="2026-09-30"),
        _row(ci_name="OtherDay", session_date="2026-10-01"),
    ]
    scoped = filter_schedules_by_scope(
        rows, floor="day", floor_date="2026-09-30", ci_names=["Keep", "missing"]
    )
    assert [r.ci_name for r in scoped] == ["Keep"]


def test_build_qa_scopes_labels():
    rows = [
        _row(session_date="2026-09-30", provisioning_date="30/09/2026 10:00"),
        _row(session_date="2026-09-30", provisioning_date="30/09/2026 15:30"),
        _row(session_date="2026-10-01", provisioning_date="01/10/2026 10:00"),
    ]
    scopes = build_qa_scopes(rows)
    assert scopes["total"] == 3
    assert scopes["dates"][0]["date"] == "2026-09-30"
    assert scopes["dates"][0]["label"] == format_day_label("2026-09-30")
    assert scopes["dates"][0]["count"] == 2
    assert {b["key"] for b in scopes["dates"][0]["bands"]} == {"morning", "afternoon"}


# --- build_qa_coverage / merge_qa_coverage (Labagator scope contract) ---------


def test_coverage_full_event_lists_every_row():
    rows = [
        _row(ci_name="AM", namespace="ns1", provisioning_date="30/09/2026 10:00", session_date="2026-09-30"),
        _row(ci_name="PM", namespace="ns2", provisioning_date="30/09/2026 15:30", session_date="2026-09-30"),
    ]
    cov = build_qa_coverage(rows, floor="event")
    assert cov["floor"] == "event"
    assert cov["expected_total"] == 2
    assert {c["ci_name"] for c in cov["covered"]} == {"AM", "PM"}
    assert cov["namespaces"] == ["ns1", "ns2"]
    bands = {c["ci_name"]: c["time_band"] for c in cov["covered"]}
    assert bands == {"AM": "morning", "PM": "afternoon"}


def test_coverage_floor_day_morning_only_excludes_afternoon():
    """The drift root cause: a morning-band run must report only morning rows."""
    rows = [
        _row(ci_name="AM", provisioning_date="30/09/2026 09:00", session_date="2026-09-30"),
        _row(ci_name="PM", provisioning_date="30/09/2026 16:00", session_date="2026-09-30"),
        _row(ci_name="NextDay", provisioning_date="01/10/2026 09:00", session_date="2026-10-01"),
    ]
    cov = build_qa_coverage(rows, floor="day", floor_date="2026-09-30", time_band="morning")
    assert cov["floor"] == "day"
    assert cov["floor_date"] == "2026-09-30"
    assert cov["time_band"] == "morning"
    assert [c["ci_name"] for c in cov["covered"]] == ["AM"]
    assert cov["expected_total"] == 1


def test_coverage_namespace_subset_narrows_rows():
    rows = [
        _row(ci_name="A", namespace="ns1", session_date="2026-09-30"),
        _row(ci_name="B", namespace="ns2", session_date="2026-09-30"),
    ]
    cov = build_qa_coverage(rows, floor="event", namespaces=["ns1"])
    assert [c["ci_name"] for c in cov["covered"]] == ["A"]
    assert cov["namespaces"] == ["ns1"]


def test_coverage_ci_names_subset_recorded():
    rows = [
        _row(ci_name="Keep", session_date="2026-09-30"),
        _row(ci_name="Skip", session_date="2026-09-30"),
    ]
    cov = build_qa_coverage(rows, floor="event", ci_names=["Keep"])
    assert cov["ci_names"] == ["Keep"]
    assert [c["ci_name"] for c in cov["covered"]] == ["Keep"]


def test_merge_full_run_replaces_prior():
    prior = {"covered": [{"ci_name": "Old"}], "ci_names": ["Old"]}
    latest = build_qa_coverage(
        [_row(ci_name="New", session_date="2026-09-30")], floor="event"
    )
    merged = merge_qa_coverage(prior, latest)
    assert merged is latest
    assert [c["ci_name"] for c in merged["covered"]] == ["New"]


def test_merge_subset_unions_covered_rows():
    rows = [
        _row(ci_name="A", namespace="ns", session_date="2026-09-30"),
        _row(ci_name="B", namespace="ns", session_date="2026-09-30"),
    ]
    first = build_qa_coverage(rows, floor="event", ci_names=["A"])
    second = build_qa_coverage(rows, floor="event", ci_names=["B"])
    merged = merge_qa_coverage(first, second)
    assert {c["ci_name"] for c in merged["covered"]} == {"A", "B"}
    assert merged["expected_total"] == 2
    assert merged["ci_names"] == ["A", "B"]


def test_merge_subset_dedupes_rerun_of_same_ci():
    rows = [_row(ci_name="A", namespace="ns", session_date="2026-09-30")]
    first = build_qa_coverage(rows, floor="event", ci_names=["A"])
    again = build_qa_coverage(rows, floor="event", ci_names=["A"])
    merged = merge_qa_coverage(first, again)
    assert merged["expected_total"] == 1


def test_merge_with_no_prior_returns_latest():
    latest = build_qa_coverage(
        [_row(ci_name="A", session_date="2026-09-30")], floor="event", ci_names=["A"]
    )
    assert merge_qa_coverage(None, latest) is latest
