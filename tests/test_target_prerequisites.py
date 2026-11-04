"""Target isolation, truthful errors, and Babylon pool ownership regressions."""

import asyncio
import json
import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from api import routes
from api.server import app
from lib.tenant_cluster_capacity import check_tenant_cluster_references
from scripts.check_babylon_contract import check


def headers(target):
    return {
        "X-RHDP-Target-Cluster": target,
        "X-Forwarded-Email": "jdisrael@redhat.com",
        "X-API-Key": os.environ.get("RHDP_API_KEY", ""),
    }


def test_concurrent_targets_use_separate_credentials_and_cleanup(monkeypatch, tmp_path, make_tenant_schedule):
    monkeypatch.setattr(routes, "_schedules", [make_tenant_schedule()])
    barrier = threading.Barrier(2)
    paths = []

    def config(target_cluster=None):
        path = tmp_path / target_cluster
        path.write_text("temporary credentials")
        paths.append(path)
        return SimpleNamespace(kubeconfig_path=str(path), oc_command="oc")

    def run(args, **kwargs):
        path = Path(kwargs["env"]["KUBECONFIG"])
        barrier.wait(timeout=5)
        assert path.exists()
        return SimpleNamespace(returncode=0 if path.name == "events" else 1, stdout="", stderr="(NotFound)")

    monkeypatch.setattr(routes, "_get_config", config)
    monkeypatch.setattr(subprocess, "run", run)
    original = os.environ.get("KUBECONFIG")
    def request(target):
        with TestClient(app) as client:
            return client.post("/api/schedules/validate-namespaces", headers=headers(target)).json()
    with ThreadPoolExecutor(2) as workers:
        events, integration = list(workers.map(request, ["events", "integration"]))
    assert events["missing"] == []
    assert integration["missing"]
    assert all(not path.exists() for path in paths)
    assert os.environ.get("KUBECONFIG") == original


def test_target_access_denied_before_credentials_are_resolved(monkeypatch):
    """With a tight email allowlist, non-default targets deny before credentials."""
    monkeypatch.setenv("DEPLOY_PICKER_ALLOWED_EMAILS", "jdisrael@redhat.com,bbethell@redhat.com")
    resolve = Mock(side_effect=AssertionError("must not read credentials"))
    monkeypatch.setattr(routes, "_get_config", resolve)
    with TestClient(app) as client:
        response = client.post("/api/schedules/validate-namespaces", headers={
            "X-RHDP-Target-Cluster": "integration", "X-API-Key": os.environ.get("RHDP_API_KEY", ""),
        })
    assert response.status_code == 403
    resolve.assert_not_called()


def test_default_events_target_allowed_without_forwarded_email(monkeypatch):
    """Labagator / API-key deploys may use the Events default without OAuth headers."""
    monkeypatch.setattr(
        routes,
        "_get_config",
        lambda **kw: SimpleNamespace(kubeconfig_path=None, oc_command="oc"),
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="[]", stderr=""),
    )
    with TestClient(app) as client:
        response = client.post("/api/schedules/validate-namespaces", headers={
            "X-RHDP-Target-Cluster": "events", "X-API-Key": os.environ.get("RHDP_API_KEY", ""),
        })
    assert response.status_code != 403


def test_forbidden_pool_is_unknown_and_never_created(monkeypatch):
    monkeypatch.setattr(routes, "_get_config", lambda **kw: SimpleNamespace(kubeconfig_path=None))
    run = Mock(return_value=SimpleNamespace(returncode=1, stdout="", stderr="Forbidden: cannot get tenantclusterpools"))
    monkeypatch.setattr(subprocess, "run", run)
    with TestClient(app) as client:
        status = client.post("/api/schedules/check-pool-status", json={"cluster_cis": ["cluster.prod"]})
        applied = client.post("/api/schedules/create-tenant-cluster-pools", json={
            "cluster_cis": ["cluster.prod"], "apply_to_cluster": True,
        })
    assert status.json()["results"][0]["action_preview"] == "error"
    assert not applied.json()["results"][0]["success"]
    assert all(call.args[0][1] == "get" for call in run.call_args_list)


@pytest.mark.parametrize("managed,expected", [(True, "ready"), (False, "pool_no_capacity")])
def test_empty_reference_pool_contract(monkeypatch, make_tenant_schedule, managed, expected):
    tenant = make_tenant_schedule()
    tenant.enable_workshop_interface = managed
    def query(args, env=None):
        assert env == {"KUBECONFIG": "/target"}
        if "tenantclusterpools" in args:
            assert args[args.index("-n") + 1] == "shared-clusters"
            return {"items": [{"metadata": {"name": "reference"}, "spec": {"enabled": False}}]}
        return {"spec": {"sandboxes": [{"tenantCluster": {"componentName": "reference"}}]}}
    monkeypatch.setattr("lib.tenant_cluster_capacity._cluster_json", query)
    result = check_tenant_cluster_references([tenant], env={"KUBECONFIG": "/target"})
    assert len(result[expected]) == 1


def test_all_references_required_even_with_explicit_cluster_row(monkeypatch, make_tenant_schedule):
    tenant = make_tenant_schedule()
    def query(args, env=None):
        if "tenantclusterpools" in args:
            return {"items": [{"metadata": {"name": "one"}}]}
        return {"spec": {"sandboxes": [
            {"tenantCluster": {"componentName": name}} for name in ["one", "two"]
        ]}}
    monkeypatch.setattr("lib.tenant_cluster_capacity._cluster_json", query)
    monkeypatch.setattr(routes, "validate_cluster_before_tenant", lambda *a, **kw: {
        "errors": [], "error_details": [], "relationships": [],
    })
    with pytest.raises(Exception, match="reference pool two"):
        routes._tenant_validation([tenant], SimpleNamespace(kubeconfig_path=None), fail_closed=True)


def test_slow_deploy_preflight_keeps_probe_responsive(monkeypatch, make_tenant_schedule):
    tenant = make_tenant_schedule()
    tenant.is_tenant = False
    tenant.users = 1
    monkeypatch.setattr(routes, "_schedules", [tenant])
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(routes, "_get_config", lambda **kw: SimpleNamespace(kubeconfig_path=None))
    monkeypatch.setattr(routes.cluster_targets, "resolve_and_cleanup_check", lambda *a: None)
    monkeypatch.setattr(routes, "validate_catalog_item_exists",
                        lambda *a, **kw: (True, "babylon-catalog-prod", None, None, []))
    monkeypatch.setattr(routes, "get_catalog_item_parameter_schemas", lambda *a, **kw: None)
    def limit(*args):
        entered.set()
        release.wait(3)
        return {"maximum": 0}
    monkeypatch.setattr(routes, "get_catalog_item_num_users_limit", limit)
    async def exercise():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            deploy = asyncio.create_task(client.post("/api/deploy", json={}))
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                probe = await asyncio.wait_for(client.get("/api/healthz"), timeout=0.5)
                assert probe.status_code == 200
            finally:
                release.set()
            assert (await deploy).status_code == 400
    asyncio.run(exercise())


def test_target_domain_not_host_domain(monkeypatch, tmp_path):
    path = tmp_path / "target.json"
    path.write_text(json.dumps({"clusters": [{"cluster": {"server": "https://api.ocp-us-west-2.infra.open.redhat.com:6443"}}]}))
    monkeypatch.setattr(routes.cluster_targets, "resolve_kubeconfig", lambda target: str(path))
    monkeypatch.setattr(routes, "_cached_base_domain", "wrong.example")
    assert routes._get_config(target_cluster="events").base_domain != "wrong.example"


def test_upstream_drift_requires_review():
    assert check({"observed": {"contract": "a"}, "reviewed": {"contract": "a"}})
    assert not check({"observed": {"contract": "b"}, "reviewed": {"contract": "a"}})
    assert not check({"observed": {}, "reviewed": {"contract": "a"}})
