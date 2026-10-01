"""Tests for derive_base_domain, build_resource_claim_payload, and related flow helpers."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from rhdp_flow import (
    _get_workshop_provision_readiness,
    build_resource_claim_payload,
    derive_base_domain,
    export_dry_run_manifest_yaml,
    get_catalog_item_num_users_limit,
    get_catalog_item_parameter_defaults,
    get_catalog_item_parameter_schemas,
    validate_schedule_parameter_values,
    verify_deployment,
)
from tests.conftest import make_config, make_schedule


class TestVerifyDeploymentWorkshopPath:
    """verify_deployment for workshop-UI deploys (guid is the Workshop name)."""

    def _cfg(self):
        cfg = make_config()
        cfg.dry_run = False
        cfg.base_domain = "babylon-catalog.apps.ocp-us-west-2.infra.open.redhat.com"
        cfg.kubeconfig_path = None
        return cfg

    def test_neither_resourceclaim_nor_workshop_found_returns_no_url(self):
        """When neither the ResourceClaim nor the Workshop exists, return url=None
        so the caller can report an honest failure (not a fabricated URL)."""
        rc_miss = MagicMock(returncode=1, stdout="", stderr="NotFound: resourceclaim")
        ws_miss = MagicMock(returncode=1, stdout="", stderr="NotFound: workshop")
        with patch("rhdp_flow.subprocess.run", side_effect=[rc_miss, ws_miss]):
            healthy, url, _ = verify_deployment("ws-abc123", "user-ns", "my.ci.prod", self._cfg())
        assert healthy is False
        assert url is None

    def test_workshop_present_returns_unverified_url(self):
        """ResourceClaim missing but the Workshop exists -> present-but-unverified
        (constructed URL, not healthy)."""
        rc_miss = MagicMock(returncode=1, stdout="", stderr="NotFound: resourceclaim")
        ws_hit = MagicMock(returncode=0, stdout=json.dumps({"metadata": {"name": "ws-abc123"}}), stderr="")
        with patch("rhdp_flow.subprocess.run", side_effect=[rc_miss, ws_hit]):
            healthy, url, _ = verify_deployment("ws-abc123", "user-ns", "my.ci.prod", self._cfg())
        assert healthy is False
        assert url and "babylon-catalog.apps.ocp-us-west-2.infra.open.redhat.com" in url


class TestWorkshopProvisionReadiness:
    """QA must wait for WorkshopProvision instances, not Workshop existence."""

    def _cfg(self):
        cfg = make_config()
        cfg.dry_run = False
        return cfg

    def test_provisioning_workshop_is_not_ready(self):
        payload = {
            "items": [{
                "metadata": {"name": "wp-one"},
                "spec": {"count": 1},
                "status": {"activeCount": 0, "provisioningCount": 1, "failedCount": 0},
            }]
        }
        command = MagicMock(returncode=0, stdout=json.dumps(payload), stderr="")
        with patch("rhdp_flow.subprocess.run", return_value=command):
            result = _get_workshop_provision_readiness("ns", "ci.prod", self._cfg())
        assert result["exists"] is True
        assert result["healthy"] is True
        assert result["ready"] is False
        assert "provisioning" in result["reason"]

    def test_all_requested_instances_active_is_ready(self):
        payload = {
            "items": [{
                "metadata": {"name": "wp-one"},
                "spec": {"count": 2},
                "status": {"activeCount": 2, "provisioningCount": 0, "failedCount": 0},
            }]
        }
        command = MagicMock(returncode=0, stdout=json.dumps(payload), stderr="")
        with patch("rhdp_flow.subprocess.run", return_value=command):
            result = _get_workshop_provision_readiness("ns", "ci.prod", self._cfg())
        assert result["healthy"] is True
        assert result["ready"] is True
        assert result["reason"] == "2/2 instances active"


class TestDeriveBaseDomain:
    """Tests for derive_base_domain."""

    def test_api_integration_demo_redhat(self):
        """Standard integration cluster URL."""
        url = "https://api.integration.demo.redhat.com:6443"
        assert derive_base_domain(url) == "integration.demo.redhat.com"

    def test_api_with_trailing_slash(self):
        """Trailing slash is stripped."""
        url = "https://api.integration.demo.redhat.com:6443/"
        assert derive_base_domain(url) == "integration.demo.redhat.com"

    def test_ocp_infra_open_redhat_com(self):
        """ocp-*.infra.open.redhat.com uses the cluster's babylon-catalog route."""
        url = "https://api.ocp-us-west-2.infra.open.redhat.com:6443"
        assert (
            derive_base_domain(url)
            == "babylon-catalog.apps.ocp-us-west-2.infra.open.redhat.com"
        )

    def test_empty_returns_fallback(self):
        """Empty or None returns fallback."""
        assert derive_base_domain("") == "integration.demo.redhat.com"
        assert derive_base_domain(None) == "integration.demo.redhat.com"

    def test_production_domain(self):
        """Production cluster returns demo.redhat.com."""
        url = "https://api.demo.redhat.com:6443"
        assert derive_base_domain(url) == "demo.redhat.com"

    def test_ocp4_infra_pattern(self):
        """ocp4-*.infra.open.redhat.com uses the cluster's babylon-catalog route."""
        url = "https://api.ocp4-staging.infra.open.redhat.com:6443"
        assert (
            derive_base_domain(url)
            == "babylon-catalog.apps.ocp4-staging.infra.open.redhat.com"
        )

    def test_unknown_host_returns_host_without_api_prefix(self):
        """Unknown host without api. prefix is returned as-is (or fallback if empty)."""
        url = "https://api.custom.openshift.com:6443"
        assert derive_base_domain(url) == "custom.openshift.com"


class TestBuildResourceClaimPayload:
    """Tests for build_resource_claim_payload."""

    def test_minimal_schedule_produces_valid_payload(self):
        """Minimal WorkshopSchedule produces a payload with required fields."""
        schedule = make_schedule(
            ci_name="Test Workshop",
            ci="openshift-cnv.ocp-virt-roadshow-multi-user.prod",
            namespace="user-bbethell-redhat-com",
            provisioning_date="15/02/2026 11:00",
            auto_stop="15/02/2026 19:00",
            auto_destroy="17/02/2026 11:00",
        )
        payload = build_resource_claim_payload(schedule)
        assert payload.get("kind") == "ResourceClaim"
        assert payload.get("apiVersion", "").startswith("poolboy")
        meta = payload.get("metadata", {})
        assert meta.get("generateName", "").startswith("openshift-cnv")
        assert meta.get("namespace") == "user-bbethell-redhat-com"
        ann = meta.get("annotations", {})
        assert "demo.redhat.com/requester" in ann
        spec = payload.get("spec", {})
        assert "provider" in spec
        assert spec["provider"].get("name") == schedule.ci
        assert "lifespan" in spec

    def test_namespace_drives_requester_email(self):
        """Requester email is derived from namespace user-*-*-* format."""
        schedule = make_schedule(namespace="user-jdoe-redhat-com")
        payload = build_resource_claim_payload(schedule)
        ann = payload.get("metadata", {}).get("annotations", {})
        assert ann.get("demo.redhat.com/requester") == "jdoe@redhat.com"
        assert ann.get("demo.redhat.com/orderedBy") == "jdoe@redhat.com"


class TestGetCatalogItemNumUsersLimit:
    """Tests for get_catalog_item_num_users_limit."""

    @patch("subprocess.run")
    def test_returns_maximum_from_schema(self, mock_run):
        """Returns maximum/minimum/default from openAPIV3Schema."""
        ci_json = {
            "spec": {
                "parameters": [
                    {
                        "name": "num_users",
                        "openAPIV3Schema": {
                            "type": "integer",
                            "default": 2,
                            "minimum": 2,
                            "maximum": 40,
                        },
                    }
                ]
            }
        }
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps(ci_json), stderr=""
        )
        config = make_config()
        result = get_catalog_item_num_users_limit("test.ci.prod", config)
        assert result is not None
        assert result["has_num_users"] is True
        assert result["maximum"] == 40
        assert result["minimum"] == 2
        assert result["default"] == 2

    @patch("subprocess.run")
    def test_returns_false_when_no_num_users(self, mock_run):
        """Returns has_num_users=False when CI has no num_users parameter."""
        ci_json = {
            "spec": {
                "parameters": [
                    {"name": "other_param", "openAPIV3Schema": {"type": "string"}}
                ]
            }
        }
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps(ci_json), stderr=""
        )
        config = make_config()
        result = get_catalog_item_num_users_limit("test.ci.prod", config)
        assert result is not None
        assert result["has_num_users"] is False
        assert result["maximum"] is None

    @patch("subprocess.run")
    def test_returns_none_when_cluster_unreachable(self, mock_run):
        """Returns None when oc command fails (cluster unreachable)."""
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
        config = make_config()
        result = get_catalog_item_num_users_limit("test.ci.prod", config)
        assert result is None

    @patch("subprocess.run")
    def test_searches_provider_spec(self, mock_run):
        """Finds num_users in spec.providerSpec.parameterDefinitions."""
        ci_json = {
            "spec": {
                "parameters": [],
                "providerSpec": {
                    "parameterDefinitions": [
                        {
                            "name": "num_users",
                            "openAPIV3Schema": {
                                "type": "integer",
                                "maximum": 100,
                            },
                        }
                    ]
                },
            }
        }
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps(ci_json), stderr=""
        )
        config = make_config()
        result = get_catalog_item_num_users_limit("test.ci.prod", config)
        assert result is not None
        assert result["has_num_users"] is True
        assert result["maximum"] == 100


class TestGetCatalogItemParameterDefaults:
    """Tests for get_catalog_item_parameter_defaults."""

    @patch("subprocess.run")
    def test_collects_openapi_defaults(self, mock_run):
        ci_json = {
            "spec": {
                "parameters": [
                    {
                        "name": "aws_region",
                        "openAPIV3Schema": {"type": "string", "default": "us-east-2"},
                    },
                    {
                        "name": "ocp4_fips_enable",
                        "openAPIV3Schema": {"type": "boolean", "default": False},
                    },
                    {"name": "no_default", "openAPIV3Schema": {"type": "string"}},
                ],
            }
        }
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps(ci_json), stderr=""
        )
        config = make_config()
        result = get_catalog_item_parameter_defaults("test.ci.prod", config)
        assert result["aws_region"] == "us-east-2"
        assert result["ocp4_fips_enable"] is False
        assert "no_default" not in result

    @patch("subprocess.run")
    def test_provider_spec_definitions(self, mock_run):
        ci_json = {
            "spec": {
                "parameters": [],
                "providerSpec": {
                    "parameterDefinitions": [
                        {
                            "name": "aws_region",
                            "openAPIV3Schema": {"default": "eu-west-1"},
                        }
                    ]
                },
            }
        }
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps(ci_json), stderr=""
        )
        config = make_config()
        result = get_catalog_item_parameter_defaults("test.ci.prod", config)
        assert result["aws_region"] == "eu-west-1"

    @patch("subprocess.run")
    def test_returns_empty_when_unreadable(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="nope")
        config = make_config()
        assert get_catalog_item_parameter_defaults("missing.ci.prod", config) == {}


class TestGetCatalogItemParameterSchemas:
    """Tests for get_catalog_item_parameter_schemas."""

    @patch("subprocess.run")
    def test_collects_enum_and_required(self, mock_run):
        ci_json = {
            "spec": {
                "parameters": [
                    {
                        "name": "aws_region",
                        "required": True,
                        "openAPIV3Schema": {
                            "type": "string",
                            "default": "us-east-2",
                            "enum": ["us-east-2"],
                        },
                    },
                    {
                        "name": "run_e2e_load_test",
                        "openAPIV3Schema": {"type": "boolean", "default": False},
                    },
                    {"name": "needs_value", "required": True, "openAPIV3Schema": {"type": "string"}},
                ]
            }
        }
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(ci_json), stderr="")
        config = make_config()
        result = get_catalog_item_parameter_schemas("test.ci.prod", config)
        assert result is not None
        assert result["aws_region"]["enum"] == ["us-east-2"]
        assert result["aws_region"]["required"] is True
        assert result["aws_region"]["has_default"] is True
        assert result["needs_value"]["required"] is True
        assert result["needs_value"]["has_default"] is False
        assert result["run_e2e_load_test"]["enum"] is None

    @patch("subprocess.run")
    def test_returns_none_when_unreachable(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
        config = make_config()
        assert get_catalog_item_parameter_schemas("missing.ci.prod", config) is None


class TestValidateScheduleParameterValues:
    """Tests for validate_schedule_parameter_values."""

    def test_region_not_in_enum_is_error(self):
        """The lb1161 case: aws_region=us-east-1 but enum=[us-east-2]."""
        schema = {"aws_region": {"enum": ["us-east-2"], "required": True, "has_default": True}}
        s = make_schedule(ci="summit-2026.lb1161-sovereign-cloud-cnv.event", aws_regions="us-east-1")
        result = validate_schedule_parameter_values(s, schema)
        assert len(result["errors"]) == 1
        err = result["errors"][0]
        assert err["parameter"] == "aws_region"
        assert err["value"] == "us-east-1"
        assert err["allowed"] == ["us-east-2"]
        assert result["warnings"] == []

    def test_region_in_enum_passes(self):
        schema = {"aws_region": {"enum": ["us-east-2"], "required": True, "has_default": True}}
        s = make_schedule(aws_regions="us-east-2")
        result = validate_schedule_parameter_values(s, schema)
        assert result["errors"] == []
        assert result["warnings"] == []

    def test_underscore_region_normalised(self):
        """CSV 'us_east_2' normalises to 'us-east-2' (matches flow's own normalisation)."""
        schema = {"aws_region": {"enum": ["us-east-2"], "has_default": True}}
        s = make_schedule(aws_regions="us_east_2")
        assert validate_schedule_parameter_values(s, schema)["errors"] == []

    def test_multi_region_each_checked(self):
        schema = {"aws_region": {"enum": ["us-east-2"], "has_default": True}}
        s = make_schedule(aws_regions="us-east-2,us-west-1")
        result = validate_schedule_parameter_values(s, schema)
        assert len(result["errors"]) == 1
        assert result["errors"][0]["value"] == "us-west-1"

    def test_no_enum_means_no_error(self):
        schema = {"aws_region": {"enum": None, "has_default": True}}
        s = make_schedule(aws_regions="us-east-1")
        assert validate_schedule_parameter_values(s, schema)["errors"] == []

    def test_region_set_but_param_absent_warns(self):
        s = make_schedule(aws_regions="us-east-1")
        result = validate_schedule_parameter_values(s, {"other": {"enum": None}})
        assert result["errors"] == []
        assert len(result["warnings"]) == 1
        assert result["warnings"][0]["parameter"] == "aws_region"

    def test_required_without_default_warns(self):
        schema = {"secret_key": {"required": True, "has_default": False, "enum": None}}
        s = make_schedule(aws_regions="")
        result = validate_schedule_parameter_values(s, schema)
        assert any(w["parameter"] == "secret_key" for w in result["warnings"])

    def test_required_with_default_is_fine(self):
        schema = {"aws_region": {"required": True, "has_default": True, "enum": None}}
        s = make_schedule(aws_regions="")
        assert validate_schedule_parameter_values(s, schema)["warnings"] == []

    def test_none_schema_returns_empty(self):
        s = make_schedule(aws_regions="us-east-1")
        assert validate_schedule_parameter_values(s, None) == {"errors": [], "warnings": []}


def test_export_dry_run_manifest_yaml_writes(tmp_path):
    """Dry-run YAML export writes a file and strips internal keys."""
    config = make_config(dry_run=True)
    config.dry_run_export_yaml_dir = str(tmp_path)
    config.dry_run_yaml_export_seq = 0
    manifest = {"kind": "ResourceClaim", "apiVersion": "poolboy.gpte.redhat.com/v1", "_white_glove": True}
    out = export_dry_run_manifest_yaml(config, "resourceclaim-test.ci", manifest)
    assert out and Path(out).exists()
    text = Path(out).read_text()
    assert "ResourceClaim" in text
    assert "_white_glove" not in text
    assert config.dry_run_yaml_export_seq == 1
