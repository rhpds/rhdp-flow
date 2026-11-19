"""
Tests for Flow labels (flow.demo.redhat.com) on ResourceClaims, Workshops, and WorkshopProvisions.
"""
from rhdp_flow import (
    RHDPConfig,
    WorkshopSchedule,
    build_resource_claim_payload,
    build_workshop_provision_dict,
    build_workshop_resource_dict,
)


def test_cluster_item_gets_cluster_label():
    """Cluster catalog items get flow.demo.redhat.com/item-type=cluster label."""
    schedule = WorkshopSchedule(
        ci_name="OCP4 Cluster",
        ci="ocp4-cluster.prod",
        namespace="user-test",
        enable_workshop_interface=False,
        password="test123",
        activity="Admin",
        purpose="QA",
        workshop_name="Test Cluster",
        provisioning_date="01/08/2026 10:00",
        auto_stop="01/08/2026 18:00",
        auto_destroy="02/08/2026 10:00",
        is_cluster=True,
        is_tenant=False,
        detected_cluster_ci=None,
        detection_method="naming",
    )

    config = RHDPConfig()
    payload = build_resource_claim_payload(schedule, config)

    assert payload["metadata"]["labels"]["flow.demo.redhat.com/item-type"] == "cluster"
    assert "flow.demo.redhat.com/cluster-ci" not in payload["metadata"]["labels"]


def test_tenant_item_gets_tenant_label_and_cluster_ci():
    """Tenant catalog items get flow.demo.redhat.com/item-type=tenant and cluster-ci labels."""
    schedule = WorkshopSchedule(
        ci_name="App Tenant",
        ci="app-tenant.prod",
        namespace="user-test",
        enable_workshop_interface=False,
        password="test123",
        activity="Admin",
        purpose="QA",
        workshop_name="Test Tenant",
        provisioning_date="01/08/2026 10:00",
        auto_stop="01/08/2026 18:00",
        auto_destroy="02/08/2026 10:00",
        is_cluster=False,
        is_tenant=True,
        detected_cluster_ci="app-cluster.prod",
        detection_method="naming",
    )

    config = RHDPConfig()
    payload = build_resource_claim_payload(schedule, config)

    assert payload["metadata"]["labels"]["flow.demo.redhat.com/item-type"] == "tenant"
    assert payload["metadata"]["labels"]["flow.demo.redhat.com/cluster-ci"] == "app-cluster.prod"


def test_tenant_with_override_cluster_ci():
    """Tenant with cluster_ci_override uses override value."""
    schedule = WorkshopSchedule(
        ci_name="App Tenant",
        ci="app-tenant.prod",
        namespace="user-test",
        enable_workshop_interface=False,
        password="test123",
        activity="Admin",
        purpose="QA",
        workshop_name="Test Tenant",
        provisioning_date="01/08/2026 10:00",
        auto_stop="01/08/2026 18:00",
        auto_destroy="02/08/2026 10:00",
        is_cluster=False,
        is_tenant=True,
        detected_cluster_ci="app-cluster.prod",
        cluster_ci_override="custom-cluster.prod",
        detection_method="naming",
    )

    config = RHDPConfig()
    payload = build_resource_claim_payload(schedule, config)

    assert payload["metadata"]["labels"]["flow.demo.redhat.com/item-type"] == "tenant"
    assert payload["metadata"]["labels"]["flow.demo.redhat.com/cluster-ci"] == "custom-cluster.prod"


def test_workshop_item_gets_workshop_label():
    """Regular workshop items get flow.demo.redhat.com/item-type=workshop label."""
    schedule = WorkshopSchedule(
        ci_name="Ansible Lab",
        ci="ansible-lab.prod",
        namespace="user-test",
        enable_workshop_interface=True,
        password="test123",
        activity="Admin",
        purpose="QA",
        workshop_name="Test Workshop",
        provisioning_date="01/08/2026 10:00",
        auto_stop="01/08/2026 18:00",
        auto_destroy="02/08/2026 10:00",
        is_cluster=False,
        is_tenant=False,
        detected_cluster_ci=None,
        detection_method="none",
    )

    config = RHDPConfig()
    payload = build_resource_claim_payload(schedule, config)

    assert payload["metadata"]["labels"]["flow.demo.redhat.com/item-type"] == "workshop"
    assert "flow.demo.redhat.com/cluster-ci" not in payload["metadata"]["labels"]


def test_workshop_resource_inherits_flow_labels():
    """Workshop resource copies Flow labels from ResourceClaim payload."""
    schedule = WorkshopSchedule(
        ci_name="App Tenant",
        ci="app-tenant.prod",
        namespace="user-test",
        enable_workshop_interface=True,
        password="test123",
        activity="Admin",
        purpose="QA",
        workshop_name="Test Workshop",
        provisioning_date="01/08/2026 10:00",
        auto_stop="01/08/2026 18:00",
        auto_destroy="02/08/2026 10:00",
        is_cluster=False,
        is_tenant=True,
        detected_cluster_ci="app-cluster.prod",
        detection_method="naming",
    )

    config = RHDPConfig()
    resourceclaim_payload = build_resource_claim_payload(schedule, config)

    workshop = build_workshop_resource_dict(
        "test-workshop",
        "user-test",
        resourceclaim_payload,
        config,
        redirect=True,
    )

    assert workshop["metadata"]["labels"]["flow.demo.redhat.com/item-type"] == "tenant"
    assert workshop["metadata"]["labels"]["flow.demo.redhat.com/cluster-ci"] == "app-cluster.prod"


def test_workshop_provision_inherits_flow_labels():
    """WorkshopProvision resource copies Flow labels from ResourceClaim payload."""
    schedule = WorkshopSchedule(
        ci_name="App Tenant",
        ci="app-tenant.prod",
        namespace="user-test",
        enable_workshop_interface=True,
        password="test123",
        activity="Admin",
        purpose="QA",
        workshop_name="Test Workshop",
        provisioning_date="01/08/2026 10:00",
        auto_stop="01/08/2026 18:00",
        auto_destroy="02/08/2026 10:00",
        is_cluster=False,
        is_tenant=True,
        detected_cluster_ci="app-cluster.prod",
        detection_method="naming",
    )

    config = RHDPConfig()
    resourceclaim_payload = build_resource_claim_payload(schedule, config)

    provision = build_workshop_provision_dict(
        "test-workshop",
        "user-test",
        resourceclaim_payload,
        config,
        concurrency=1,
        count=1,
        extra_parameters=None,
        fetch_catalog_defaults=False,
    )

    assert provision["metadata"]["labels"]["flow.demo.redhat.com/item-type"] == "tenant"
    assert provision["metadata"]["labels"]["flow.demo.redhat.com/cluster-ci"] == "app-cluster.prod"


def test_cluster_flow_labels_propagate_through_all_resources():
    """Verify Flow labels propagate from ResourceClaim -> Workshop -> WorkshopProvision for cluster."""
    schedule = WorkshopSchedule(
        ci_name="OCP4 Cluster",
        ci="ocp4-cluster.prod",
        namespace="user-test",
        enable_workshop_interface=True,
        password="test123",
        activity="Admin",
        purpose="QA",
        workshop_name="Test Cluster",
        provisioning_date="01/08/2026 10:00",
        auto_stop="01/08/2026 18:00",
        auto_destroy="02/08/2026 10:00",
        is_cluster=True,
        is_tenant=False,
        detected_cluster_ci=None,
        detection_method="naming",
    )

    config = RHDPConfig()

    # Build all three resources
    resourceclaim = build_resource_claim_payload(schedule, config)
    workshop = build_workshop_resource_dict(
        "test-workshop",
        "user-test",
        resourceclaim,
        config,
        redirect=True,
    )
    provision = build_workshop_provision_dict(
        "test-workshop",
        "user-test",
        resourceclaim,
        config,
        concurrency=1,
        count=1,
        extra_parameters=None,
        fetch_catalog_defaults=False,
    )

    # All three should have the same Flow label
    assert resourceclaim["metadata"]["labels"]["flow.demo.redhat.com/item-type"] == "cluster"
    assert workshop["metadata"]["labels"]["flow.demo.redhat.com/item-type"] == "cluster"
    assert provision["metadata"]["labels"]["flow.demo.redhat.com/item-type"] == "cluster"

    # None should have cluster-ci (they ARE the cluster)
    assert "flow.demo.redhat.com/cluster-ci" not in resourceclaim["metadata"]["labels"]
    assert "flow.demo.redhat.com/cluster-ci" not in workshop["metadata"]["labels"]
    assert "flow.demo.redhat.com/cluster-ci" not in provision["metadata"]["labels"]


def test_labagator_event_id_stamped_on_resourceclaim_and_workshop():
    """CSV Event_Id becomes labagator.redhat.com/event-id on RC + Workshop + WP."""
    schedule = WorkshopSchedule(
        ci_name="Ansible Lab",
        ci="ansible-lab.prod",
        namespace="user-test",
        enable_workshop_interface=True,
        password="test123",
        activity="Admin",
        purpose="QA",
        workshop_name="Test Workshop",
        provisioning_date="01/08/2026 10:00",
        auto_stop="01/08/2026 18:00",
        auto_destroy="02/08/2026 10:00",
        event_id="32",
    )
    config = RHDPConfig()
    rc = build_resource_claim_payload(schedule, config)
    assert rc["metadata"]["labels"]["labagator.redhat.com/event-id"] == "32"
    workshop = build_workshop_resource_dict(
        "test-workshop", "user-test", rc, config, redirect=True,
    )
    assert workshop["metadata"]["labels"]["labagator.redhat.com/event-id"] == "32"
    provision = build_workshop_provision_dict(
        "test-workshop", "user-test", rc, config,
        concurrency=1, count=1, extra_parameters=None, fetch_catalog_defaults=False,
    )
    assert provision["metadata"]["labels"]["labagator.redhat.com/event-id"] == "32"


def test_labagator_event_id_absent_when_blank():
    schedule = WorkshopSchedule(
        ci_name="Ansible Lab",
        ci="ansible-lab.prod",
        namespace="user-test",
        enable_workshop_interface=False,
        password="test123",
        activity="Admin",
        purpose="QA",
        workshop_name="Test Workshop",
        provisioning_date="01/08/2026 10:00",
        auto_stop="01/08/2026 18:00",
        auto_destroy="02/08/2026 10:00",
        event_id="",
    )
    payload = build_resource_claim_payload(schedule, RHDPConfig())
    assert "labagator.redhat.com/event-id" not in payload["metadata"]["labels"]


def test_labagator_session_labels_stamped_on_resourceclaim_and_workshop():
    """CSV Session Date + Session_Start become labagator session labels (HHMM)."""
    schedule = WorkshopSchedule(
        ci_name="Ansible Lab",
        ci="ansible-lab.prod",
        namespace="user-test",
        enable_workshop_interface=True,
        password="test123",
        activity="Admin",
        purpose="QA",
        workshop_name="Test Workshop",
        provisioning_date="01/08/2026 10:00",
        auto_stop="01/08/2026 18:00",
        auto_destroy="02/08/2026 10:00",
        session_date="2026-06-02",
        session_start="14:30",
        event_id="32",
    )
    config = RHDPConfig()
    rc = build_resource_claim_payload(schedule, config)
    assert rc["metadata"]["labels"]["labagator.redhat.com/event-id"] == "32"
    assert rc["metadata"]["labels"]["labagator.redhat.com/session-date"] == "2026-06-02"
    assert rc["metadata"]["labels"]["labagator.redhat.com/session-start"] == "1430"
    workshop = build_workshop_resource_dict(
        "test-workshop", "user-test", rc, config, redirect=True,
    )
    assert workshop["metadata"]["labels"]["labagator.redhat.com/session-date"] == "2026-06-02"
    assert workshop["metadata"]["labels"]["labagator.redhat.com/session-start"] == "1430"
    provision = build_workshop_provision_dict(
        "test-workshop", "user-test", rc, config,
        concurrency=1, count=1, extra_parameters=None, fetch_catalog_defaults=False,
    )
    assert provision["metadata"]["labels"]["labagator.redhat.com/session-start"] == "1430"


def test_labagator_session_labels_absent_when_blank():
    schedule = WorkshopSchedule(
        ci_name="Ansible Lab",
        ci="ansible-lab.prod",
        namespace="user-test",
        enable_workshop_interface=False,
        password="test123",
        activity="Admin",
        purpose="QA",
        workshop_name="Test Workshop",
        provisioning_date="01/08/2026 10:00",
        auto_stop="01/08/2026 18:00",
        auto_destroy="02/08/2026 10:00",
        event_id="32",
    )
    payload = build_resource_claim_payload(schedule, RHDPConfig())
    assert payload["metadata"]["labels"]["labagator.redhat.com/event-id"] == "32"
    assert "labagator.redhat.com/session-date" not in payload["metadata"]["labels"]
    assert "labagator.redhat.com/session-start" not in payload["metadata"]["labels"]
