"""Persist schedules/results across restarts (PVC-backed data dir)."""

from dataclasses import dataclass

from lib import flow_state


@dataclass
class _Sched:
    ci_name: str
    ci: str
    namespace: str = "ns"


@dataclass
class _Result:
    ci_name: str
    ci: str
    status: str = "failed"


def test_schedules_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("RHDP_FLOW_DATA_DIR", str(tmp_path))
    flow_state.save_schedules([_Sched("A", "a.prod"), _Sched("B", "b.event")], filename="summit.csv")
    loaded, name = flow_state.load_schedules(_Sched)
    assert name == "summit.csv"
    assert len(loaded) == 2
    assert loaded[0].ci == "a.prod"
    assert loaded[1].ci_name == "B"


def test_results_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("RHDP_FLOW_DATA_DIR", str(tmp_path))
    flow_state.save_results([_Result("A", "a.prod", "failed")])
    loaded = flow_state.load_results(_Result)
    assert len(loaded) == 1
    assert loaded[0].status == "failed"


def test_clear_persisted_state(tmp_path, monkeypatch):
    monkeypatch.setenv("RHDP_FLOW_DATA_DIR", str(tmp_path))
    flow_state.save_schedules([_Sched("A", "a.prod")], filename="x.csv")
    flow_state.save_results([_Result("A", "a.prod")])
    flow_state.save_qa_results([{"ci_name": "A", "ci": "a.prod", "namespace": "ns", "status": "ok"}])
    flow_state.clear_persisted_state()
    assert flow_state.load_schedules(_Sched) == ([], "")
    assert flow_state.load_results(_Result) == []
    assert flow_state.load_qa_results() == []


def test_qa_results_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("RHDP_FLOW_DATA_DIR", str(tmp_path))
    rows = [{"ci_name": "A", "ci": "a.prod", "namespace": "ns", "status": "VERIFIED"}]
    flow_state.save_qa_results(rows)
    assert flow_state.load_qa_results() == rows
