"""Durable multi-workspace isolation (schedules / results / jobs)."""

from __future__ import annotations

import io
import os

import pytest

# Force sqlite before importing app modules
os.environ.setdefault("RHDP_ALLOW_UNAUTHENTICATED", "true")
os.environ["RHDP_DATABASE_URL"] = "sqlite:////tmp/rhdp-flow-workspace-tests.db"
os.environ.pop("RHDP_REQUIRE_SECRET_KEY", None)

from fastapi.testclient import TestClient

from api.db import reset_db_for_tests
from api.server import app
from api import jobs
from api.workspace_store import current_workspace_id, load, update
from rhdp_flow import WorkshopSchedule

CSV = (
    "CI Name,CI,Namespace,Users,Enable_workshop_interface,Password,Activity,Purpose,"
    "Provisioning Date (UTC),Auto-stop (UTC),Auto-destroy (UTC)\n"
    "My Lab,my.ci.prod,user-ns,10,true,pw,Workshop,Demo,"
    "01/06/2026 14:00,01/06/2026 22:00,15/06/2026 14:00\n"
)


@pytest.fixture(autouse=True)
def _fresh_db():
    reset_db_for_tests()
    yield


@pytest.fixture
def client():
    return TestClient(app)


def _sched(name: str) -> WorkshopSchedule:
    return WorkshopSchedule(
        ci_name=name,
        ci=f"{name}.prod",
        namespace="ns",
        enable_workshop_interface=True,
        password="secret",
        activity="W",
        purpose="D",
        workshop_name=name,
        provisioning_date="01/01/2026 00:00",
        auto_stop="01/01/2026 01:00",
        auto_destroy="02/01/2026 00:00",
    )


def test_workspaces_do_not_clobber(client: TestClient):
    def mut_a(data):
        data.schedules = [_sched("A")]

    def mut_b(data):
        data.schedules = [_sched("B")]

    update(mut_a, "event-1")
    update(mut_b, "event-2")
    assert [s.ci_name for s in load("event-1").schedules] == ["A"]
    assert [s.ci_name for s in load("event-2").schedules] == ["B"]

    r1 = client.get("/api/schedules", headers={"X-RHDP-Workspace": "event-1"})
    r2 = client.get("/api/schedules", headers={"X-RHDP-Workspace": "event-2"})
    assert r1.status_code == 200 and r1.json()[0]["ci_name"] == "A"
    assert r2.status_code == 200 and r2.json()[0]["ci_name"] == "B"


def test_upload_is_workspace_scoped(client: TestClient):
    up = client.post(
        "/api/schedules/upload",
        files={"file": ("s.csv", io.BytesIO(CSV.encode()), "text/csv")},
        headers={"X-RHDP-Workspace": "event-42"},
    )
    assert up.status_code == 200
    assert up.json()["count"] == 1
    assert len(client.get("/api/schedules", headers={"X-RHDP-Workspace": "event-42"}).json()) == 1
    assert client.get("/api/schedules", headers={"X-RHDP-Workspace": "event-99"}).json() == []


def test_jobs_interrupt_on_restart():
    j = jobs.create_job("event-7")
    jobs.update_job(j.job_id, status=jobs.Status.running)
    n = jobs.interrupt_nonterminal_jobs()
    assert n >= 1
    assert jobs.get_job(j.job_id).status == jobs.Status.interrupted


def test_passwords_round_trip_encrypted():
    current_workspace_id.set("event-sec")

    def mut(data):
        data.schedules = [_sched("Sec")]

    update(mut, "event-sec")
    loaded = load("event-sec").schedules[0]
    assert loaded.password == "secret"


def test_qa_results_reload_as_objects_not_dicts():
    """QA merge uses r.namespace; bare dicts from JSON must be rehydrated."""
    from api.models import QAResultItem

    def mut(data):
        data.qa_results = [
            QAResultItem(
                ci_name="Lab A",
                ci="lab-a.prod",
                namespace="event-42",
                status="pass",
                qa_type="1",
            ),
            {
                "ci_name": "Lab B",
                "ci": "lab-b.prod",
                "namespace": "event-42",
                "status": "pass",
                "qa_type": "1",
                "workshop_users_assigned": "3/10",
            },
        ]

    update(mut, "event-qa")
    loaded = load("event-qa").qa_results
    assert len(loaded) == 2
    assert all(hasattr(r, "namespace") for r in loaded)
    assert loaded[0].namespace == "event-42"
    assert loaded[1].workshop_users_assigned == "3/10"
    # Attribute access used by QA2 merge after QA1 persisted
    keys = {(r.namespace, r.ci_name, getattr(r, "qa_type", "")) for r in loaded}
    assert ("event-42", "Lab A", "1") in keys
