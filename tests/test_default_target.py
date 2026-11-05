"""Requests without an explicit target run against the default cluster (Events).

Flow runs on infra01, which has no Babylon catalog namespaces or
TenantClusterPool CRD. Any path that silently fell back to the in-cluster
ServiceAccount returned empty catalogs, "not found" items and 502s.
"""

import os
import subprocess
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api import cluster_targets, identity, routes
from api.server import app

DEFAULT = identity.DEFAULT_TARGET_CLUSTER


@pytest.fixture
def default_configured(monkeypatch):
    monkeypatch.setattr(cluster_targets, "is_configured", lambda target: target == DEFAULT)


@pytest.fixture
def recorded_config(monkeypatch, tmp_path):
    """Replace _get_config with a recorder that writes a temp kubeconfig per target."""
    calls = []

    def fake(target_cluster=None, **kwargs):
        calls.append(target_cluster)
        path = None
        if target_cluster:
            path = tmp_path / f"kc-{target_cluster}-{len(calls)}"
            path.write_text("creds")
        return SimpleNamespace(kubeconfig_path=str(path) if path else None, base_domain="x", oc_command="true")

    monkeypatch.setattr(routes, "_get_config", fake)
    return calls


def _client():
    return TestClient(app, headers={"X-API-Key": os.environ.get("RHDP_API_KEY", "")})


# -- effective_target --------------------------------------------------------

def test_explicit_target_wins(default_configured):
    assert identity.effective_target("prod") == "prod"


def test_no_target_uses_default_when_configured(default_configured):
    assert identity.effective_target(None) == DEFAULT
    assert identity.effective_target("") == DEFAULT
    assert identity.effective_target("   ") == DEFAULT


def test_no_target_falls_back_to_in_cluster_when_default_missing():
    # conftest pins is_configured -> False (CI / local dev without the Secret)
    assert identity.effective_target(None) is None


# -- is_configured -----------------------------------------------------------

def test_is_configured_never_raises_without_oc(monkeypatch):
    monkeypatch.undo()  # drop the conftest stub; exercise the real function
    cluster_targets._reset_configured_cache()

    def no_oc(args):
        raise FileNotFoundError("oc")

    monkeypatch.setattr(cluster_targets, "_oc", no_oc)
    assert cluster_targets.is_configured("events") is False


def test_is_configured_caches(monkeypatch):
    monkeypatch.undo()
    cluster_targets._reset_configured_cache()
    calls = []

    def fake_oc(args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "secret/cluster-events", "")

    monkeypatch.setattr(cluster_targets, "_oc", fake_oc)
    assert cluster_targets.is_configured("events") is True
    assert cluster_targets.is_configured("events") is True
    assert len(calls) == 1
    cluster_targets._reset_configured_cache()


# -- routes ------------------------------------------------------------------

def test_request_config_routes_default_to_events(default_configured, recorded_config, monkeypatch, make_tenant_schedule):
    """validate/catalog/pool endpoints use _request_config; no target -> Events."""
    monkeypatch.setattr(routes, "_schedules", [make_tenant_schedule()])
    r = _client().post("/api/schedules/validate-namespaces", json={})
    assert r.status_code == 200
    assert recorded_config == [DEFAULT]


def test_request_config_cleans_up_default_target_kubeconfig(default_configured, recorded_config, tmp_path):
    _client().post("/api/schedules/validate-namespaces", json={})
    assert not list(tmp_path.glob("kc-*")), "temp kubeconfig leaked"


def test_request_config_without_default_never_deletes_user_kubeconfig(monkeypatch, tmp_path):
    kc = tmp_path / "my-kubeconfig"
    kc.write_text("mine")
    monkeypatch.setenv("KUBECONFIG", str(kc))
    monkeypatch.setattr(routes, "_detect_and_cache_base_domain", lambda: "example.com")
    _client().post("/api/schedules/validate-namespaces", json={})
    assert kc.exists()


def test_dry_run_defaults_to_events(default_configured, recorded_config, monkeypatch, make_tenant_schedule):
    s = make_tenant_schedule()
    s.multi_workshop_name = None
    monkeypatch.setattr(routes, "_schedules", [s])
    monkeypatch.setattr(routes, "process_schedule", lambda sched, config, **kw: routes.DeploymentResult(
        ci_name=sched.ci_name, ci=sched.ci, namespace=sched.namespace, guid="g", url="u",
        status="dry_run", provisioning_date="", auto_stop="", auto_destroy="", timestamp="",
    ))
    monkeypatch.setattr(routes, "_save_results", lambda *a, **kw: None)
    r = _client().post("/api/deploy/dry-run", json={"target_cluster": None})
    assert r.status_code == 200, r.text
    assert recorded_config == [DEFAULT]
