"""Tests for POST /schedules/ensure-namespaces and event-* ProjectRequest create."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from api.server import app
import api.routes as routes
from rhdp_flow import WorkshopSchedule


def _make_schedule(**overrides) -> WorkshopSchedule:
    base = dict(
        ci_name="Lab",
        ci="lab.prod",
        namespace="event-42-17112026-0900",
        enable_workshop_interface=True,
        password="x",
        activity="Admin",
        purpose="QA",
        workshop_name="Lab",
        provisioning_date="17/11/2026 09:00",
        auto_stop="17/11/2026 12:00",
        auto_destroy="18/11/2026 09:00",
        event_name="Summit Test",
        session_date="2026-11-17",
        session_start="09:00",
    )
    base.update(overrides)
    return WorkshopSchedule(**base)


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_workspace(monkeypatch):
    monkeypatch.setenv("RHDP_ALLOW_UNAUTHENTICATED", "true")
    monkeypatch.setenv("RHDP_DATABASE_URL", "sqlite:////tmp/rhdp-flow-ensure-ns-tests.db")
    from api.db import reset_db_for_tests

    reset_db_for_tests()
    from api.workspace_store import current_workspace_id, ensure_workspace, update

    current_workspace_id.set("default")
    ensure_workspace("default")

    def clear(data):
        data.schedules = []

    update(clear, "default")
    yield


def test_ensure_namespaces_creates_missing_via_projectrequest(client):
    from api.workspace_store import update

    def mut(data):
        data.schedules = [
            _make_schedule(
                namespace="event-42-17112026-0900",
                event_name="Summit Test",
                session_date="2026-11-17",
                session_start="09:00",
            )
        ]

    update(mut, "default")

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if list(cmd)[:3] == ["oc", "get", "namespace"]:
            return MagicMock(returncode=1, stdout="", stderr="NotFound")
        if list(cmd)[:2] == ["oc", "create"] and "-f" in cmd:
            return MagicMock(
                returncode=0,
                stdout="project.project.openshift.io/event-42-17112026-0900 created",
                stderr="",
            )
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("api.routes.subprocess.run", side_effect=fake_run):
        with patch("api.routes._get_config") as mock_cfg:
            from rhdp_flow import RHDPConfig

            cfg = RHDPConfig()
            cfg.oc_command = "oc"
            cfg.kubeconfig_path = None
            mock_cfg.return_value = cfg
            resp = client.post(
                "/api/schedules/ensure-namespaces",
                headers={"X-RHDP-Workspace": "default"},
            )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["created"] == ["event-42-17112026-0900"]
    assert body["missing"] == []
    assert body["failed"] == []
    assert any(c[:2] == ["oc", "create"] and "-f" in c for c in calls)
    assert not any(c[:3] == ["oc", "create", "namespace"] for c in calls)
    assert any(
        c[:3] == ["oc", "label", "namespace"]
        and any("usernamespace.gpte.redhat.com/user-uid=EVENT" in x for x in c)
        for c in calls
    )
    assert any(
        c[:3] == ["oc", "annotate", "namespace"]
        and any("openshift.io/display-name=Summit Test — 17/11/2026 09:00" in x for x in c)
        for c in calls
    )


def test_ensure_namespaces_does_not_create_user_namespaces(client):
    from api.workspace_store import update

    def mut(data):
        data.schedules = [_make_schedule(namespace="user-klewis-redhat-com")]

    update(mut, "default")

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if list(cmd)[:3] == ["oc", "get", "namespace"]:
            return MagicMock(returncode=1, stdout="", stderr="NotFound")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("api.routes.subprocess.run", side_effect=fake_run):
        with patch("api.routes._get_config") as mock_cfg:
            from rhdp_flow import RHDPConfig

            cfg = RHDPConfig()
            cfg.oc_command = "oc"
            mock_cfg.return_value = cfg
            resp = client.post(
                "/api/schedules/ensure-namespaces",
                headers={"X-RHDP-Workspace": "default"},
            )

    assert resp.status_code == 200
    body = resp.json()
    assert body["created"] == []
    assert body["missing"] == ["user-klewis-redhat-com"]
    assert not any(c[:2] == ["oc", "create"] for c in calls)


def test_session_block_display_name_format():
    assert (
        routes._session_block_display_name("Summit", "2026-11-18", "09:00", "event-1")
        == "Summit — 18/11/2026 09:00"
    )
    assert (
        routes._session_block_display_name("", None, None, "event-42-24112026-1000")
        == "Labagator event (event-42-24112026-1000)"
    )


def test_deploy_results_all_failed():
    from rhdp_flow import DeploymentResult

    failed = DeploymentResult(
        ci_name="A",
        ci="a.prod",
        namespace="event-1",
        guid="failed",
        url="",
        status="failed",
        provisioning_date="",
        auto_stop="",
        auto_destroy="",
        timestamp="",
        error_message="namespaces \"event-1\" not found",
    )
    ok = DeploymentResult(
        ci_name="B",
        ci="b.prod",
        namespace="event-1",
        guid="ws-1",
        url="https://x",
        status="verified",
        provisioning_date="",
        auto_stop="",
        auto_destroy="",
        timestamp="",
    )
    assert routes._deploy_results_all_failed([failed, failed]) is True
    assert routes._deploy_results_all_failed([failed, ok]) is False
    assert routes._deploy_results_all_failed([]) is False
