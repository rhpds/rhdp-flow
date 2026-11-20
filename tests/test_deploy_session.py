"""Tests for POST /api/deploy/session — per-session deploy.

The endpoint parses an explicitly-uploaded CSV into a LOCAL schedule list and
deploys THAT list. Schedules/results are written only into the caller's
workspace (``X-RHDP-Workspace``), never another event's.
"""

import io
import os
import sys

os.environ.setdefault("RHDP_ALLOW_UNAUTHENTICATED", "true")
os.environ.setdefault("RHDP_DATABASE_URL", "sqlite:////tmp/rhdp-flow-deploy-session-tests.db")

# Ensure project root is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import patch

from fastapi.testclient import TestClient

from api.db import reset_db_for_tests
from api.jobs import Job, Status
from api.server import app
from api.workspace_store import load

client = TestClient(app)

CSV = (
    "CI Name,CI,Namespace,Users,Enable_workshop_interface,Password,Activity,Purpose,"
    "Provisioning Date (UTC),Auto-stop (UTC),Auto-destroy (UTC)\n"
    "My Lab,my.ci.prod,user-ns,10,true,pw,Workshop,Demo,"
    "01/06/2026 14:00,01/06/2026 22:00,15/06/2026 14:00\n"
)


def setup_function():
    reset_db_for_tests()


def test_deploy_session_stages_into_workspace_only():
    with patch("api.routes.jobs.create_job", return_value=Job(job_id="abc123def456", status=Status.pending, workspace_id="event-5")), \
         patch("api.routes.asyncio.create_task"):  # don't actually run the background deploy
        resp = client.post(
            "/api/deploy/session",
            files={"file": ("s.csv", io.BytesIO(CSV.encode()), "text/csv")},
            data={"dry_run": "true"},
            headers={"X-RHDP-Workspace": "event-5"},
        )
    assert resp.status_code == 200
    assert resp.json()["job_id"] == "abc123def456"
    assert resp.json().get("workspace_id") == "event-5"
    assert len(load("event-5").schedules) == 1
    assert load("event-5").schedules[0].ci == "my.ci.prod"
    assert load("event-9").schedules == []


def test_deploy_session_requires_api_key(monkeypatch):
    monkeypatch.setenv("RHDP_API_KEY", "secret")
    resp = client.post(
        "/api/deploy/session",
        files={"file": ("s.csv", io.BytesIO(CSV.encode()), "text/csv")},
    )
    # Repo convention: verify_api_key raises 403 (not 401) on a missing key.
    assert resp.status_code == 403  # missing X-API-Key
