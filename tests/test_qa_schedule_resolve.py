"""QA schedule auto-load / Workshop discovery for label-scoped Ops runs."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from api.services.qa_schedule_resolve import (
    discover_schedules_by_labagator_labels,
    ensure_label_scoped_schedules,
    fetch_labagator_schedules,
    merge_schedules_for_event,
    parse_schedules_from_csv_text,
    qa_scope_error_message,
    workshop_cr_to_schedule,
)
from rhdp_flow import WorkshopSchedule


def _sched(**kwargs) -> WorkshopSchedule:
    defaults = dict(
        ci_name="Lab A",
        ci="org.lab-a.test",
        namespace="ns-a",
        enable_workshop_interface=True,
        password="",
        activity="Admin",
        purpose="QA",
        workshop_name="Lab A",
        provisioning_date="19/11/2026 10:00",
        auto_stop="",
        auto_destroy="",
        session_date="2026-11-19",
        session_start="1000",
        event_id="42",
    )
    defaults.update(kwargs)
    return WorkshopSchedule(**defaults)


def test_qa_scope_error_message_points_at_send_to_flow():
    msg = qa_scope_error_message(event_id="42", session_date="2026-11-19")
    assert "event_id=42" in msg
    assert "session_date=2026-11-19" in msg
    assert "Send to Flow" in msg


def test_merge_schedules_for_event_replaces_only_that_event():
    existing = [
        _sched(ci_name="Keep", event_id="99", namespace="ns-other"),
        _sched(ci_name="Old42", event_id="42", namespace="ns-old"),
    ]
    incoming = [_sched(ci_name="New42", event_id="42", namespace="ns-new")]
    merged = merge_schedules_for_event(existing, incoming, "42")
    assert [s.ci_name for s in merged] == ["Keep", "New42"]


def test_workshop_cr_to_schedule_reads_labagator_labels():
    ws = {
        "metadata": {
            "name": "ws-1",
            "namespace": "user-ops",
            "labels": {
                "babylon.gpte.redhat.com/catalogItemName": "org.lab-a.test",
                "babylon.gpte.redhat.com/catalogItemNamespace": "babylon-catalog-event",
                "labagator.redhat.com/event-id": "42",
                "labagator.redhat.com/session-date": "2026-11-19",
                "labagator.redhat.com/session-start": "1530",
            },
            "annotations": {
                "babylon.gpte.redhat.com/catalogItemDisplayName": "Lab A",
                "demo.redhat.com/purpose": "QA",
                "demo.redhat.com/purpose-activity": "Admin",
            },
        },
        "spec": {
            "displayName": "Lab A",
            "accessPassword": "secret",
            "labUserInterface": {"redirect": True},
            "actionSchedule": {"start": "2026-11-19T10:00:00Z", "stop": ""},
            "lifespan": {"end": "2026-11-19T18:00:00Z"},
        },
    }
    s = workshop_cr_to_schedule(ws)
    assert s is not None
    assert s.namespace == "user-ops"
    assert s.ci == "org.lab-a.test"
    assert s.event_id == "42"
    assert s.session_date == "2026-11-19"
    assert s.session_start == "1530"
    assert s.provisioning_date == "19/11/2026 10:00"


_MIN_FLOW_CSV = """\
CI Name,CI,Namespace,Users,Enable_workshop_interface,Password,Activity,Purpose,Workshop Name,Provisioning Date (UTC),Auto-stop (UTC),Auto-destroy (UTC),Session Date,Session_Start,Event_Id
Lab A,org.lab-a.test,ns-a,5,True,,Admin,QA,Lab A,19/11/2026 10:00,19/11/2026 18:00,20/11/2026 10:00,2026-11-19,10:00,42
"""


def test_parse_schedules_from_csv_text_reads_event_and_session_date():
    rows = parse_schedules_from_csv_text(_MIN_FLOW_CSV)
    assert len(rows) == 1
    assert rows[0].event_id == "42"
    assert rows[0].session_date == "2026-11-19"
    assert rows[0].namespace == "ns-a"


@patch("api.services.qa_schedule_resolve.labagator_client.get_deploy_handoff_csv")
def test_fetch_labagator_schedules_passes_filter_date(mock_csv):
    mock_csv.return_value = _MIN_FLOW_CSV
    rows = fetch_labagator_schedules("42", session_date="2026-11-19")
    assert len(rows) == 1
    mock_csv.assert_called_once()
    assert mock_csv.call_args.kwargs.get("filter_date") == "2026-11-19"
    assert mock_csv.call_args.kwargs.get("event_id") == 42


@patch("api.services.qa_schedule_resolve.subprocess.run")
def test_discover_schedules_by_labagator_labels(mock_run):
    mock_run.return_value = SimpleNamespace(
        returncode=0,
        stdout=(
            '{"items":[{"metadata":{"name":"ws","namespace":"ns-a","labels":{'
            '"babylon.gpte.redhat.com/catalogItemName":"org.lab-a.test",'
            '"labagator.redhat.com/event-id":"42",'
            '"labagator.redhat.com/session-date":"2026-11-19"},'
            '"annotations":{}},"spec":{"displayName":"Lab A","labUserInterface":{},'
            '"actionSchedule":{},"lifespan":{}}}]}'
        ),
        stderr="",
    )
    config = MagicMock(kubeconfig_path=None, oc_command="oc", timeout=30)
    rows = discover_schedules_by_labagator_labels(config, "42", session_date="2026-11-19")
    assert len(rows) == 1
    assert rows[0].event_id == "42"
    assert "-l" in mock_run.call_args.args[0]
    selector = mock_run.call_args.args[0][mock_run.call_args.args[0].index("-l") + 1]
    assert "labagator.redhat.com/event-id=42" in selector
    assert "labagator.redhat.com/session-date=2026-11-19" in selector


@patch("api.services.qa_schedule_resolve.fetch_labagator_schedules")
@patch("api.services.qa_schedule_resolve.discover_schedules_by_labagator_labels")
def test_ensure_auto_loads_from_labagator_when_empty(mock_discover, mock_fetch):
    mock_fetch.return_value = [_sched()]
    mock_discover.return_value = []
    updated, scoped = ensure_label_scoped_schedules(
        [],
        event_id="42",
        floor="day",
        floor_date="2026-11-19",
        get_config=lambda: MagicMock(),
    )
    assert len(updated) == 1
    assert len(scoped) == 1
    mock_fetch.assert_called_once()
    mock_discover.assert_not_called()


@patch("api.services.qa_schedule_resolve.fetch_labagator_schedules", return_value=[])
@patch("api.services.qa_schedule_resolve.discover_schedules_by_labagator_labels")
def test_ensure_falls_back_to_workshop_discovery(mock_discover, _mock_fetch):
    mock_discover.return_value = [_sched(ci_name="FromCluster")]
    updated, scoped = ensure_label_scoped_schedules(
        [],
        event_id="42",
        floor="day",
        floor_date="2026-11-19",
        get_config=lambda: MagicMock(),
    )
    assert [s.ci_name for s in scoped] == ["FromCluster"]
    mock_discover.assert_called_once()
