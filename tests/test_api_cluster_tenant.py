"""Integration tests for POST /schedules/validate-cluster-tenant."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.server import app
from api.workspace_store import ws

client = TestClient(app)


def _auth_headers():
    import os
    return {"X-API-Key": os.environ.get("RHDP_API_KEY", "")}


@pytest.mark.usefixtures("legacy_tenant_catalog")
class TestValidateClusterTenantEndpoint:
    """Tests for the cluster-tenant validation endpoint using rhdp_flow's validator."""

    def test_no_schedules_loaded(self):
        """Endpoint returns 400 when no schedules loaded."""
        client.post("/api/sessions/clear", headers=_auth_headers())
        response = client.post("/api/schedules/validate-cluster-tenant", headers=_auth_headers())
        assert response.status_code == 400
        assert "No schedules loaded" in response.json()["detail"]

    def test_uses_rhdp_flow_validator_and_reports_out_of_batch_error(self, monkeypatch):
        """Test that endpoint uses rhdp_flow validator and reports error for tenant without cluster in batch."""
        from rhdp_flow import WorkshopSchedule

        tenant = WorkshopSchedule(
            ci_name="Tenant One",
            ci="ocp4-tenant.prod",
            namespace="ns",
            enable_workshop_interface=True,
            password="x",
            activity="Admin",
            purpose="QA",
            workshop_name="w",
            provisioning_date="10/09/2026 10:00",
            auto_stop="11/09/2026 10:00",
            auto_destroy="12/09/2026 10:00",
            item_type="tenant",
            is_tenant=True,
            detected_cluster_ci="ocp4-cluster.prod",
        )
        ws.schedules = [tenant]
        monkeypatch.setattr("rhdp_flow.find_provisioned_cluster_resourceclaim", lambda ci, cfg: False)

        response = client.post("/api/schedules/validate-cluster-tenant", headers=_auth_headers())

        assert response.status_code == 200
        body = response.json()
        assert len(body["errors"]) == 1
        assert body["errors"][0]["tenant_ci"] == "ocp4-tenant.prod"
        assert "neither in this batch nor already provisioned" in body["errors"][0]["message"]
        assert body["errors"][0]["cluster_ci"] == "ocp4-cluster.prod"

    def test_valid_ordering_no_errors(self, monkeypatch):
        """Cluster scheduled before tenant → no errors."""
        from rhdp_flow import WorkshopSchedule

        cluster = WorkshopSchedule(
            ci_name="Cluster One",
            ci="ocp4-cluster.prod",
            namespace="ns",
            enable_workshop_interface=True,
            password="x",
            activity="Admin",
            purpose="QA",
            workshop_name="w",
            provisioning_date="10/09/2026 10:00",
            auto_stop="11/09/2026 10:00",
            auto_destroy="12/09/2026 10:00",
            item_type="cluster",
            is_cluster=True,
        )
        tenant = WorkshopSchedule(
            ci_name="Tenant One",
            ci="ocp4-tenant.prod",
            namespace="ns",
            enable_workshop_interface=True,
            password="x",
            activity="Admin",
            purpose="QA",
            workshop_name="w",
            provisioning_date="10/09/2026 11:00",  # 1 hour after cluster
            auto_stop="11/09/2026 10:00",
            auto_destroy="12/09/2026 10:00",
            item_type="tenant",
            is_tenant=True,
            detected_cluster_ci="ocp4-cluster.prod",
        )
        ws.schedules = [cluster, tenant]

        response = client.post("/api/schedules/validate-cluster-tenant", headers=_auth_headers())

        assert response.status_code == 200
        body = response.json()
        assert body["errors"] == []
        assert body["tenants_checked"] == 1
        assert body["clusters_found"] == 1

    def test_invalid_ordering_tenant_before_cluster(self, monkeypatch):
        """Tenant scheduled before cluster → timing error."""
        from rhdp_flow import WorkshopSchedule

        cluster = WorkshopSchedule(
            ci_name="Cluster One",
            ci="ocp4-cluster.prod",
            namespace="ns",
            enable_workshop_interface=True,
            password="x",
            activity="Admin",
            purpose="QA",
            workshop_name="w",
            provisioning_date="10/09/2026 11:00",  # AFTER tenant
            auto_stop="11/09/2026 10:00",
            auto_destroy="12/09/2026 10:00",
            item_type="cluster",
            is_cluster=True,
        )
        tenant = WorkshopSchedule(
            ci_name="Tenant One",
            ci="ocp4-tenant.prod",
            namespace="ns",
            enable_workshop_interface=True,
            password="x",
            activity="Admin",
            purpose="QA",
            workshop_name="w",
            provisioning_date="10/09/2026 10:00",  # BEFORE cluster
            auto_stop="11/09/2026 10:00",
            auto_destroy="12/09/2026 10:00",
            item_type="tenant",
            is_tenant=True,
            detected_cluster_ci="ocp4-cluster.prod",
        )
        ws.schedules = [cluster, tenant]

        response = client.post("/api/schedules/validate-cluster-tenant", headers=_auth_headers())

        assert response.status_code == 200
        body = response.json()
        assert len(body["errors"]) == 1
        assert body["errors"][0]["tenant_ci"] == "ocp4-tenant.prod"
        assert body["errors"][0]["cluster_ci"] == "ocp4-cluster.prod"
        assert "must be provisioned first" in body["errors"][0]["message"].lower()
        assert body["tenants_checked"] == 1

    def test_tenant_without_cluster_in_batch_gives_warning_when_no_config(self, monkeypatch):
        """Tenant without cluster in batch → warning (when live lookup unavailable)."""
        from rhdp_flow import WorkshopSchedule

        tenant = WorkshopSchedule(
            ci_name="Tenant One",
            ci="ocp4-tenant.prod",
            namespace="ns",
            enable_workshop_interface=True,
            password="x",
            activity="Admin",
            purpose="QA",
            workshop_name="w",
            provisioning_date="10/09/2026 10:00",
            auto_stop="11/09/2026 10:00",
            auto_destroy="12/09/2026 10:00",
            item_type="tenant",
            is_tenant=True,
            detected_cluster_ci="ocp4-cluster.prod",
        )
        ws.schedules = [tenant]
        # Simulate lookup failure (no config or lookup returns None)
        monkeypatch.setattr("rhdp_flow.find_provisioned_cluster_resourceclaim", lambda ci, cfg: None)

        response = client.post("/api/schedules/validate-cluster-tenant", headers=_auth_headers())

        assert response.status_code == 200
        body = response.json()
        assert body["errors"] == []
        assert len(body["warnings"]) == 1
        assert body["warnings"][0]["tenant_ci"] == "ocp4-tenant.prod"
        assert "ocp4-cluster.prod" in body["warnings"][0]["message"]
        assert body["tenants_checked"] == 1
        assert body["clusters_found"] == 0


class TestScheduleResponseIncludesDetectionFields:
    def test_schedules_endpoint_includes_cluster_tenant_fields(self, monkeypatch):
        from rhdp_flow import WorkshopSchedule

        tenant = WorkshopSchedule(
            ci_name="Tenant One", ci="ocp4-tenant.prod", namespace="ns",
            enable_workshop_interface=True, password="x", activity="Admin",
            purpose="QA", workshop_name="w", provisioning_date="10/09/2026 10:00",
            auto_stop="11/09/2026 10:00", auto_destroy="12/09/2026 10:00",
            is_tenant=True, detected_cluster_ci="ocp4-cluster.prod",
            detection_method="naming", cluster_ci_source="naming",
        )
        ws.schedules = [tenant]

        response = client.get("/api/schedules", headers=_auth_headers())

        assert response.status_code == 200
        body = response.json()[0]
        assert body["is_tenant"] is True
        assert body["detected_cluster_ci"] == "ocp4-cluster.prod"
        assert body["detection_method"] == "naming"
        assert body["cluster_ci_source"] == "naming"
