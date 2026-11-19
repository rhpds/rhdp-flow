"""Tests for POST /schedules/ensure-namespaces."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from api.server import app
import api.routes as routes
from tests.conftest import make_schedule


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_schedules():
    with routes._state_lock:
        routes._schedules = []
    yield
    with routes._state_lock:
        routes._schedules = []


def test_ensure_namespaces_creates_missing(client, monkeypatch):
    monkeypatch.setenv("RHDP_ALLOW_UNAUTHENTICATED", "true")
    with routes._state_lock:
        routes._schedules = [make_schedule(namespace="event-42-17112026")]

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if list(cmd)[:3] == ["oc", "get", "namespace"]:
            return MagicMock(returncode=1, stdout="", stderr="NotFound")
        if list(cmd)[:3] == ["oc", "create", "namespace"]:
            return MagicMock(returncode=0, stdout="namespace/event-42-17112026 created", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("api.routes.subprocess.run", side_effect=fake_run):
        with patch("api.routes._get_config") as mock_cfg:
            from rhdp_flow import RHDPConfig

            cfg = RHDPConfig()
            cfg.oc_command = "oc"
            cfg.kubeconfig_path = None
            mock_cfg.return_value = cfg
            resp = client.post("/api/schedules/ensure-namespaces")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["created"] == ["event-42-17112026"]
    assert body["missing"] == []
    assert body["failed"] == []
    assert any(c[:3] == ["oc", "create", "namespace"] for c in calls)


def test_ensure_namespaces_reports_forbidden(client, monkeypatch):
    monkeypatch.setenv("RHDP_ALLOW_UNAUTHENTICATED", "true")
    with routes._state_lock:
        routes._schedules = [make_schedule(namespace="event-7-05012027")]

    def fake_run(cmd, **kwargs):
        if list(cmd)[:3] == ["oc", "get", "namespace"]:
            return MagicMock(returncode=1, stdout="", stderr="NotFound")
        if list(cmd)[:3] == ["oc", "create", "namespace"]:
            return MagicMock(
                returncode=1,
                stdout="",
                stderr="Error from server (Forbidden): namespaces is forbidden",
            )
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("api.routes.subprocess.run", side_effect=fake_run):
        with patch("api.routes._get_config") as mock_cfg:
            from rhdp_flow import RHDPConfig

            cfg = RHDPConfig()
            cfg.oc_command = "oc"
            mock_cfg.return_value = cfg
            resp = client.post("/api/schedules/ensure-namespaces")

    assert resp.status_code == 200
    body = resp.json()
    assert body["created"] == []
    assert body["missing"] == ["event-7-05012027"]
    assert body["failed"] and "Forbidden" in body["failed"][0]["error"]
