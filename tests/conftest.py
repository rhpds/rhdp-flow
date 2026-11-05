"""Shared pytest fixtures for RHDP-Flow tests."""

import json
import os
import subprocess
import sys
import tempfile
import time

import pytest

# Ensure rhdp_flow is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Tests run without a configured API key; opt out of the fail-closed gate so
# the keyless test suite exercises endpoints as before. Individual tests that
# assert auth behavior set RHDP_API_KEY (or clear this) explicitly.
os.environ.setdefault("RHDP_ALLOW_UNAUTHENTICATED", "true")

from rhdp_flow import (
    RHDPConfig,
    WorkshopSchedule,
)


@pytest.fixture(autouse=True)
def _default_target_not_configured(monkeypatch):
    """Keep target resolution hermetic: no ``cluster-<default>`` Secret exists.

    ``identity.effective_target`` falls back to the default cluster (Events)
    only when its Secret is found via ``oc``. Without this, a developer whose
    local ``oc`` is logged into the real cluster would hit live credentials
    in tests. Tests that exercise the default-target path override this.
    """
    from api import cluster_targets

    cluster_targets._reset_configured_cache()
    monkeypatch.setattr(cluster_targets, "is_configured", lambda target: False)


@pytest.fixture(autouse=True)
def _drain_background_jobs():
    """Drain in-flight background jobs (QA/deploy) after every test, suite-wide.

    Jobs run as async tasks on the shared TestClient anyio portal loop and
    outlive the request that started them. A job writes its results to the
    module-global stores *before* it flips to a terminal status, so once no
    job is pending/running every write has landed. Draining here — for every
    test in every file, not just the ones that assert on jobs — stops an
    orphaned task from a prior test bleeding a late write into the next
    test's freshly reset globals.
    """
    import logging as _logging

    def _purge_rhdp_file_handlers():
        _logger = _logging.getLogger("rhdp_flow")
        for _h in list(_logger.handlers):
            if isinstance(_h, _logging.FileHandler):
                _logger.removeHandler(_h)
                try:
                    _h.close()
                except Exception:
                    pass

    # Start every test with a clean logger: any per-run FileHandler still
    # attached from an earlier test's job would make this test's workers fan
    # their log records out to stale handlers, which under concurrency can wedge
    # a background job. Purge on the way in as well as out.
    _purge_rhdp_file_handlers()
    yield

    from api import jobs

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        pending = [
            j for j in list(jobs._jobs.values())
            if j.status in (jobs.Status.pending, jobs.Status.running)
        ]
        if not pending:
            break
        time.sleep(0.02)

    # Detach per-run FileHandlers left on the shared ``rhdp_flow`` logger. Each
    # QA/deploy job attaches one via start_log_capture and removes it on
    # completion — but a job that outlived its test (or timed out above) never
    # does. Left in place they accumulate across the suite, so every later
    # worker's ``logger.info`` fans out to dozens of stale handlers, which under
    # concurrency intermittently wedges a background job.
    _purge_rhdp_file_handlers()


# ============================================================================
# CSV Fixture Constants
# ============================================================================

BASIC_WORKSHOP_CSV = """\
CI Name,CI,Namespace,Users,Enable_workshop_interface,Password,Activity,Purpose,Workshop Name,Provisioning Date (UTC),Auto-stop (UTC),Auto-destroy (UTC),Multi_Asset,Asset_CIs,Multi_Workshop_Name,Concurrency,Instances,Salesforce IDs
Experience OpenShift Virtualization Roadshow,openshift-cnv.ocp-virt-roadshow-multi-user.prod,user-bbethell-redhat-com,20,True,Workshop1,Admin,QA,Virt Roadshow Basic,15/02/2026 11:00,15/02/2026 19:00,17/02/2026 11:00,,,,,,2,
"""

MULTI_ASSET_OLD_CSV = """\
CI Name,CI,Namespace,Users,Enable_workshop_interface,Password,Activity,Purpose,Workshop Name,Provisioning Date (UTC),Auto-stop (UTC),Auto-destroy (UTC),Multi_Asset,Asset_CIs,Multi_Workshop_Name,Concurrency,Instances,Salesforce IDs
Multi Asset Workshop,openshift-cnv.ocp-virt-roadshow-multi-user.prod,user-bbethell-redhat-com,20,True,Pass1,Admin,QA,Summit Multi,19/02/2026 10:00,19/02/2026 18:00,21/02/2026 10:00,True,"openshift-cnv.ocp-virt-roadshow-multi-user.prod,zt-ansiblebu.ansible-network-automation-basics-lab-2.event",summit-multi-2026,,,
"""

MULTI_ASSET_GROUPED_CSV = """\
CI Name,CI,Namespace,Users,Enable_workshop_interface,Password,Activity,Purpose,Workshop Name,Provisioning Date (UTC),Auto-stop (UTC),Auto-destroy (UTC),Multi_Asset,Asset_CIs,Multi_Workshop_Name,Concurrency,Instances,Salesforce IDs
Virt Roadshow Asset,openshift-cnv.ocp-virt-roadshow-multi-user.prod,user-bbethell-redhat-com,20,True,VirtPass1,Admin,QA,Summit Demo,19/02/2026 10:00,19/02/2026 18:00,21/02/2026 10:00,,,summit-demo-2026,,,
Ansible Lab Asset,zt-ansiblebu.ansible-network-automation-basics-lab-2.event,user-bbethell-redhat-com,20,True,AnsPass2,Admin,QA,Summit Demo,19/02/2026 10:00,19/02/2026 18:00,21/02/2026 10:00,,,summit-demo-2026,,,
"""

INSTANCES_AND_CONCURRENCY_CSV = """\
CI Name,CI,Namespace,Users,Enable_workshop_interface,Password,Activity,Purpose,Workshop Name,Provisioning Date (UTC),Auto-stop (UTC),Auto-destroy (UTC),Multi_Asset,Asset_CIs,Multi_Workshop_Name,Concurrency,Instances,Salesforce IDs
OpenShift AI Workshop,openshift-ai.ai-workshop-multi-user.prod,user-bbethell-redhat-com,40,True,AIPass1,Admin,Demo,AI Workshop,17/02/2026 10:00,17/02/2026 18:00,19/02/2026 10:00,,,,3,30,
"""

OLD_DATE_HEADERS_CSV = """\
CI Name,CI,Namespace,Users,Enable_workshop_interface,Password,Activity,Purpose,Workshop Name,Provisioning Date,Auto-stop,Auto-destroy,Multi_Asset,Asset_CIs,Multi_Workshop_Name,Concurrency,Instances,Salesforce IDs
Basic Workshop,openshift-cnv.ocp-virt-roadshow-multi-user.prod,user-bbethell-redhat-com,20,True,Pass1,Admin,QA,Old Headers,15/02/2026 11:00,15/02/2026 19:00,17/02/2026 11:00,,,,,,
"""

MISSING_HEADERS_CSV = """\
CI Name,CI,Namespace
Basic Workshop,some-ci,some-ns
"""

SHOWROOM_CSV = """\
CI Name,CI,Namespace,Users,Enable_workshop_interface,Password,Activity,Purpose,Workshop Name,Provisioning Date (UTC),Auto-stop (UTC),Auto-destroy (UTC),Multi_Asset,Asset_CIs,Multi_Workshop_Name,Concurrency,Instances,Salesforce IDs,Showroom_Repo,Showroom_Ref,Showroom_NoVNC,Showroom_Zerotouch
Showroom Workshop,openshift-cnv.ocp-virt-roadshow-multi-user.prod,user-bbethell-redhat-com,20,True,Workshop1,Admin,QA,Virt Showroom,15/02/2026 11:00,15/02/2026 19:00,17/02/2026 11:00,,,,,,2,https://github.com/rhpds/showroom-virt.git,main,False,False
"""

CLUSTER_TENANT_VALID_CSV = """\
CI Name,CI,Namespace,Users,Enable_workshop_interface,Password,Activity,Purpose,Workshop Name,Provisioning Date (UTC),Auto-stop (UTC),Auto-destroy (UTC),Cluster_CI,Item_Type
Cluster Workshop,openshift-cnv.ocp-virt-roadshow-multi-user.prod,user-bbethell-redhat-com,20,True,Pass1,Admin,QA,Cluster WS,15/02/2026 11:00,15/02/2026 19:00,17/02/2026 11:00,,cluster
Tenant Workshop,openshift-cnv.ocp-virt-roadshow-multi-user.prod-tenant,user-bbethell-redhat-com,10,True,Pass2,Admin,QA,Tenant WS,15/02/2026 11:30,15/02/2026 19:00,17/02/2026 11:00,openshift-cnv.ocp-virt-roadshow-multi-user.prod,tenant
"""

CLUSTER_TENANT_WRONG_ORDER_CSV = """\
CI Name,CI,Namespace,Users,Enable_workshop_interface,Password,Activity,Purpose,Workshop Name,Provisioning Date (UTC),Auto-stop (UTC),Auto-destroy (UTC),Cluster_CI,Item_Type
Tenant Workshop,openshift-cnv.ocp-virt-roadshow-multi-user.prod-tenant,user-bbethell-redhat-com,10,True,Pass2,Admin,QA,Tenant WS,15/02/2026 11:00,15/02/2026 19:00,17/02/2026 11:00,openshift-cnv.ocp-virt-roadshow-multi-user.prod,tenant
Cluster Workshop,openshift-cnv.ocp-virt-roadshow-multi-user.prod,user-bbethell-redhat-com,20,True,Pass1,Admin,QA,Cluster WS,15/02/2026 11:30,15/02/2026 19:00,17/02/2026 11:00,,cluster
"""

CLUSTER_TENANT_MISSING_CLUSTER_CSV = """\
CI Name,CI,Namespace,Users,Enable_workshop_interface,Password,Activity,Purpose,Workshop Name,Provisioning Date (UTC),Auto-stop (UTC),Auto-destroy (UTC),Cluster_CI,Item_Type
Tenant Workshop,openshift-cnv.ocp-virt-roadshow-multi-user.prod-tenant,user-bbethell-redhat-com,10,True,Pass2,Admin,QA,Tenant WS,15/02/2026 11:00,15/02/2026 19:00,17/02/2026 11:00,openshift-cnv.ocp-virt-roadshow-multi-user.prod,tenant
"""

CLUSTER_TENANT_OVERRIDE_CSV = """\
CI Name,CI,Namespace,Users,Enable_workshop_interface,Password,Activity,Purpose,Workshop Name,Provisioning Date (UTC),Auto-stop (UTC),Auto-destroy (UTC),Cluster_CI,Item_Type
Tenant Workshop,openshift-cnv.ocp-virt-roadshow-multi-user.prod-tenant,user-bbethell-redhat-com,10,True,Pass2,Admin,QA,Tenant WS,15/02/2026 11:00,15/02/2026 19:00,17/02/2026 11:00,custom-cluster.prod,tenant
"""


# ============================================================================
# Auto-Provision Test Fixtures
# ============================================================================

@pytest.fixture
def make_tenant_schedule():
    """Factory for creating tenant WorkshopSchedule instances."""
    def _make(
        ci: str = "workshop.prod-tenant",
        ci_name: str = "Test Tenant Workshop",
        namespace: str = "test-ns",
        provisioning_date: str = "15/02/2026 11:00",
        detected_cluster_ci: str | None = None,
        cluster_ci_override: str | None = None,
    ):
        return WorkshopSchedule(
            ci_name=ci_name,
            ci=ci,
            namespace=namespace,
            users=10,
            enable_workshop_interface=True,
            password="test123",
            activity="Admin",
            purpose="QA",
            workshop_name="test-workshop",
            provisioning_date=provisioning_date,
            auto_stop="15/02/2026 19:00",
            auto_destroy="17/02/2026 11:00",
            instances=1,
            catalog_namespace="babylon-catalog-prod",
            item_type="tenant",
            is_tenant=True,
            detected_cluster_ci=detected_cluster_ci,
            cluster_ci_override=cluster_ci_override,
        )
    return _make


@pytest.fixture
def make_cluster_schedule():
    """Factory for creating cluster WorkshopSchedule instances."""
    def _make(
        ci: str = "ocp4-cluster.prod",
        ci_name: str = "Test Cluster",
        namespace: str = "test-ns",
        provisioning_date: str = "15/02/2026 10:00",
        auto_added: bool = False,
    ):
        return WorkshopSchedule(
            ci_name=ci_name,
            ci=ci,
            namespace=namespace,
            users=0,
            enable_workshop_interface=False,
            password="",
            activity="Admin",
            purpose="QA",
            workshop_name="test-workshop",
            provisioning_date=provisioning_date,
            auto_stop="15/02/2026 19:00",
            auto_destroy="17/02/2026 11:00",
            instances=1,
            catalog_namespace="babylon-catalog-prod",
            item_type="cluster",
            is_cluster=True,
            auto_added=auto_added,
        )
    return _make


@pytest.fixture
def mock_pool_list_empty():
    """Mock empty TenantClusterPool list response."""
    return {
        "returncode": 0,
        "stdout": json.dumps({"items": []}),
        "stderr": "",
    }


@pytest.fixture
def mock_pool_list_with_ready_pool():
    """Mock TenantClusterPool list with one pool that has 3 available clusters.

    Uses the real Babylon status schema: status.clusters is a list of objects
    each with a sandboxApiState field (available | pending | disabled | removed).
    """
    return {
        "returncode": 0,
        "stdout": json.dumps({
            "items": [
                {
                    "metadata": {"name": "ocp4-cluster"},
                    "status": {
                        "clusters": [
                            {"sandboxApiState": "available"},
                            {"sandboxApiState": "available"},
                            {"sandboxApiState": "available"},
                        ]
                    },
                }
            ]
        }),
        "stderr": "",
    }


# ============================================================================
# Factory functions
# ============================================================================

def make_schedule(**overrides):
    """Create a WorkshopSchedule with sensible defaults."""
    defaults = dict(
        ci_name="Experience OpenShift Virtualization Roadshow",
        ci="openshift-cnv.ocp-virt-roadshow-multi-user.prod",
        namespace="user-bbethell-redhat-com",
        users=20,
        enable_workshop_interface=True,
        password="Workshop1",
        activity="Admin",
        purpose="QA",
        workshop_name="Virt Roadshow Basic",
        provisioning_date="15/02/2026 11:00",
        auto_stop="15/02/2026 19:00",
        auto_destroy="17/02/2026 11:00",
        is_multi_asset=False,
        asset_cis="",
        multi_workshop_name="",
        concurrency=1,
        salesforce_ids="",
        redirect=True,
    )
    defaults.update(overrides)
    return WorkshopSchedule(**defaults)


def make_config(dry_run=False, kubeconfig_path=None):
    """Create an RHDPConfig with sensible defaults."""
    config = RHDPConfig()
    config.dry_run = dry_run
    config.kubeconfig_path = kubeconfig_path
    config.timeout = 10
    return config


def make_oc_dispatcher(overrides=None):
    """
    Return a callable for subprocess.run side_effect that dispatches
    based on the oc subcommand in cmd list.
    """
    overrides = overrides or {}

    def dispatcher(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        if not cmd or len(cmd) < 2:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        # Handle helm commands (Showroom deploy/teardown)
        if cmd[0] == "helm" or (isinstance(cmd[0], str) and cmd[0].endswith("helm")):
            subcmd = cmd[1] if len(cmd) > 1 else ""
            if subcmd in ("upgrade", "install"):
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="Release \"showroom\" has been upgraded.\n", stderr=""
                )
            if subcmd == "uninstall":
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="release \"showroom\" uninstalled\n", stderr=""
                )
            if subcmd == "list":
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="showroom\tdefault\t1\tdeployed\n", stderr=""
                )
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        subcmd = cmd[1]
        resource_type = cmd[2] if len(cmd) > 2 else ""
        key = (subcmd, resource_type)
        if key in overrides:
            val = overrides[key]
            return val() if callable(val) else val

        if subcmd == "version":
            return subprocess.CompletedProcess(
                cmd, 0, stdout="Client Version: 4.14.0\n", stderr=""
            )

        if subcmd == "create":
            tmpfile = None
            for i, arg in enumerate(cmd):
                if arg == "-f" and i + 1 < len(cmd):
                    tmpfile = cmd[i + 1]
                    break
            kind = "ResourceClaim"
            name_prefix = "unknown"
            if tmpfile and os.path.exists(tmpfile):
                try:
                    with open(tmpfile) as f:
                        payload = json.load(f)
                    kind = payload.get("kind", "ResourceClaim")
                    name_prefix = (
                        payload.get("metadata", {}).get("generateName", "")
                        or payload.get("metadata", {}).get("name", "unknown")
                    )
                except Exception:
                    pass

            kind_lower = kind.lower()
            api_map = {
                "resourceclaim": "resourceclaim.poolboy.gpte.redhat.com",
                "workshop": "workshop.babylon.gpte.redhat.com",
                "workshopprovision": "workshopprovision.babylon.gpte.redhat.com",
                "multiworkshop": "multiworkshop.babylon.gpte.redhat.com",
            }
            api_prefix = api_map.get(
                kind_lower, f"{kind_lower}.babylon.gpte.redhat.com"
            )
            if name_prefix.endswith("-"):
                actual_name = f"{name_prefix}abc12"
            else:
                actual_name = name_prefix
            stdout = f"{api_prefix}/{actual_name} created\n"
            return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

        if subcmd == "get":
            resource = cmd[2] if len(cmd) > 2 else ""
            if resource == "workshop":
                jsonpath_arg = None
                for arg in cmd:
                    if "jsonpath" in str(arg):
                        jsonpath_arg = arg
                        break
                if jsonpath_arg and "workshop-id" in jsonpath_arg:
                    return subprocess.CompletedProcess(cmd, 0, stdout="m5hzmw", stderr="")
                if any(a == "json" for a in cmd):
                    workshop_json = {
                        "metadata": {
                            "name": cmd[3] if len(cmd) > 3 else "test-workshop",
                            "labels": {
                                "babylon.gpte.redhat.com/workshop-id": "m5hzmw"
                            },
                        },
                        "spec": {
                            "actionSchedule": {
                                "start": "2026-02-15T11:00:00Z",
                                "stop": "2026-02-15T19:00:00Z",
                            },
                            "lifespan": {
                                "start": "2026-02-15T11:00:00Z",
                                "end": "2026-02-17T11:00:00Z",
                            },
                            "labUserInterface": {"redirect": True},
                        },
                    }
                    return subprocess.CompletedProcess(
                        cmd, 0, stdout=json.dumps(workshop_json), stderr=""
                    )
                return subprocess.CompletedProcess(
                    cmd, 0,
                    stdout="workshop.babylon.gpte.redhat.com/test-workshop",
                    stderr=""
                )

            if resource == "workshopprovision":
                if any("jsonpath" in str(a) for a in cmd):
                    return subprocess.CompletedProcess(cmd, 0, stdout="test-workshop", stderr="")
                return subprocess.CompletedProcess(
                    cmd, 0,
                    stdout=json.dumps({
                        "items": [{
                            "metadata": {"name": "test-workshop"},
                            "spec": {
                                "lifespan": {"end": "2026-02-17T11:00:00Z"},
                                "actionSchedule": {"stop": "2026-02-15T19:00:00Z"},
                                "count": 20,
                            },
                        }]
                    }),
                    stderr=""
                )

            if resource in ("resourceclaim", "resourceclaims"):
                rc_json = {
                    "metadata": {"name": "test-rc-abc12"},
                    "status": {"healthy": True, "ready": True},
                }
                if any(a == "json" for a in cmd):
                    return subprocess.CompletedProcess(
                        cmd, 0, stdout=json.dumps(rc_json), stderr=""
                    )
                return subprocess.CompletedProcess(cmd, 0, stdout="test-rc-abc12", stderr="")

            if resource == "catalogitem":
                return subprocess.CompletedProcess(
                    cmd, 0,
                    stdout="babylon-catalog-prod:Experience OpenShift Virtualization Roadshow",
                    stderr=""
                )

            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        if subcmd == "patch":
            resource = cmd[2] if len(cmd) > 2 else "resource"
            name = cmd[3] if len(cmd) > 3 else "unknown"
            return subprocess.CompletedProcess(
                cmd, 0, stdout=f"{resource}/{name} patched\n", stderr=""
            )

        if subcmd == "delete":
            return subprocess.CompletedProcess(cmd, 0, stdout="deleted\n", stderr="")

        # Showroom: handle ingresses.config/cluster for wildcard domain
        if subcmd == "get" and len(cmd) > 2 and "ingresses.config" in cmd[2]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="apps.cluster.example.com", stderr=""
            )

        # Showroom: handle pod queries for health checks
        if subcmd == "get" and len(cmd) > 2 and cmd[2] == "pod":
            pod_json = {
                "items": [{
                    "metadata": {"name": "showroom-pod-abc"},
                    "status": {
                        "phase": "Running",
                        "conditions": [{"type": "Ready", "status": "True"}],
                    },
                }]
            }
            if any(a == "json" for a in cmd):
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=json.dumps(pod_json), stderr=""
                )
            return subprocess.CompletedProcess(cmd, 0, stdout="showroom-pod-abc", stderr="")

        # Showroom: handle route queries
        if subcmd == "get" and len(cmd) > 2 and cmd[2] == "route":
            route_json = {
                "items": [{
                    "metadata": {"name": "showroom-route"},
                    "spec": {"host": "showroom.apps.cluster.example.com"},
                }]
            }
            if any(a == "json" for a in cmd):
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=json.dumps(route_json), stderr=""
                )
            return subprocess.CompletedProcess(cmd, 0, stdout="showroom-route", stderr="")

        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    return dispatcher


def write_csv_tempfile(csv_text):
    """Write CSV text to a temporary file, return path."""
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".csv", delete=False, encoding="utf-8"
    )
    tmp.write(csv_text)
    tmp.close()
    return tmp.name


# ============================================================================
# Pytest fixtures
# ============================================================================

@pytest.fixture
def sample_schedule():
    return make_schedule()


@pytest.fixture
def mock_config():
    return make_config(dry_run=True)


@pytest.fixture
def dry_config():
    return make_config(dry_run=True)


@pytest.fixture
def basic_csv_file(tmp_path):
    p = tmp_path / "basic.csv"
    p.write_text(BASIC_WORKSHOP_CSV)
    return str(p)


@pytest.fixture
def legacy_tenant_catalog(monkeypatch):
    """Legacy timing tests use catalogs without controller-managed pools."""
    monkeypatch.setattr(
        "lib.tenant_cluster_capacity._cluster_json",
        lambda args, env=None: {"items": [], "spec": {"sandboxes": []}},
    )
