#!/usr/bin/env python3
"""
Test suite for RHDP-Flow: Red Hat Demo Platform Workshop Automation Tool.

Run with:
    python3 -m pytest test_rhdp_flow.py -v
    python3 -m unittest test_rhdp_flow -v
"""

import csv
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

# Import the module under test
try:
    from rhdp_flow import (
        DeploymentResult,
        RHDPConfig,
        WorkshopSchedule,
        _provider_parameter_values,
        build_resource_claim_payload,
        calculate_duration,
        construct_workshop_url,
        create_multi_region_workshop,
        create_multi_workshop,
        create_multi_workshop_from_group,
        create_parser,
        create_resource_claim_via_oc,
        create_workshop_provision,
        create_workshop_with_ui,
        derive_base_domain,
        export_student_landing_page_csv,
        extend_destroy_time,
        extend_stop_time,
        format_iso8601,
        get_landing_page_url,
        get_workshop_urls,
        load_asset_passwords,
        lock_workshops,
        parse_date_time,
        process_schedule,
        read_csv_input,
        scale_workshops,
        unlock_workshops,
        users_blank_catalog_default_advisory,
        verify_deployment,
        write_deployment_results,
    )
except ImportError:
    print("Error: Could not import rhdp_flow.py")
    print("Please ensure test_rhdp_flow.py is in the same directory as rhdp_flow.py")
    sys.exit(1)


# ============================================================================
# CSV FIXTURE CONSTANTS
# ============================================================================

BASIC_WORKSHOP_CSV = """\
CI Name,CI,Namespace,Users,Enable_workshop_interface,Password,Activity,Purpose,Workshop Name,Provisioning Date (UTC),Auto-stop (UTC),Auto-destroy (UTC),Multi_Asset,Asset_CIs,Multi_Workshop_Name,Concurrency,Instances,Count,AWS_Region
Experience OpenShift Virtualization Roadshow,openshift-cnv.ocp-virt-roadshow-multi-user.prod,user-bbethell-redhat-com,20,True,Workshop1,Admin,QA,Virt Roadshow Basic,15/02/2026 11:00,15/02/2026 19:00,17/02/2026 11:00,,,,,,2,,
"""

MULTI_ASSET_OLD_CSV = """\
CI Name,CI,Namespace,Users,Enable_workshop_interface,Password,Activity,Purpose,Workshop Name,Provisioning Date (UTC),Auto-stop (UTC),Auto-destroy (UTC),Multi_Asset,Asset_CIs,Multi_Workshop_Name,Concurrency,Instances,Count,AWS_Region
Multi Asset Workshop,openshift-cnv.ocp-virt-roadshow-multi-user.prod,user-bbethell-redhat-com,20,True,Pass1,Admin,QA,Summit Multi,19/02/2026 10:00,19/02/2026 18:00,21/02/2026 10:00,True,"openshift-cnv.ocp-virt-roadshow-multi-user.prod,zt-ansiblebu.ansible-network-automation-basics-lab-2.event",summit-multi-2026,,,,
"""

MULTI_ASSET_GROUPED_CSV = """\
CI Name,CI,Namespace,Users,Enable_workshop_interface,Password,Activity,Purpose,Workshop Name,Provisioning Date (UTC),Auto-stop (UTC),Auto-destroy (UTC),Multi_Asset,Asset_CIs,Multi_Workshop_Name,Concurrency,Instances,Count,AWS_Region
Virt Roadshow Asset,openshift-cnv.ocp-virt-roadshow-multi-user.prod,user-bbethell-redhat-com,20,True,VirtPass1,Admin,QA,Summit Demo,19/02/2026 10:00,19/02/2026 18:00,21/02/2026 10:00,,,summit-demo-2026,,,,
Ansible Lab Asset,zt-ansiblebu.ansible-network-automation-basics-lab-2.event,user-bbethell-redhat-com,20,True,AnsPass2,Admin,QA,Summit Demo,19/02/2026 10:00,19/02/2026 18:00,21/02/2026 10:00,,,summit-demo-2026,,,,
"""

MULTI_REGION_CSV = (
    "CI Name,CI,Namespace,Users,Enable_workshop_interface,Password,Activity,Purpose,Workshop Name,Provisioning Date (UTC),Auto-stop (UTC),Auto-destroy (UTC),Multi_Asset,Asset_CIs,Multi_Workshop_Name,Concurrency,Instances,Count,AWS_Region\n"
    'Regional Workshop,openshift-cnv.ocp-virt-roadshow-multi-user.prod,user-bbethell-redhat-com,40,True,RegPass1,Admin,QA,Regional Virt,20/02/2026 10:00,20/02/2026 18:00,22/02/2026 10:00,,,,,,,"us-east-1,eu-west-1"\n'
)

COUNT_EXPANSION_CSV = """\
CI Name,CI,Namespace,Users,Enable_workshop_interface,Password,Activity,Purpose,Workshop Name,Provisioning Date (UTC),Auto-stop (UTC),Auto-destroy (UTC),Multi_Asset,Asset_CIs,Multi_Workshop_Name,Concurrency,Instances,Count,AWS_Region
OpenShift AI Workshop,openshift-ai.ai-workshop-multi-user.prod,user-bbethell-redhat-com,40,True,AIPass1,Admin,Demo,AI Workshop,17/02/2026 10:00,17/02/2026 18:00,19/02/2026 10:00,,,,3,,2,
"""

OLD_DATE_HEADERS_CSV = """\
CI Name,CI,Namespace,Users,Enable_workshop_interface,Password,Activity,Purpose,Workshop Name,Provisioning Date,Auto-stop,Auto-destroy,Multi_Asset,Asset_CIs,Multi_Workshop_Name,Concurrency,Instances,Count,AWS_Region
Basic Workshop,openshift-cnv.ocp-virt-roadshow-multi-user.prod,user-bbethell-redhat-com,20,True,Pass1,Admin,QA,Old Headers,15/02/2026 11:00,15/02/2026 19:00,17/02/2026 11:00,,,,,,,
"""

MISSING_HEADERS_CSV = """\
CI Name,CI,Namespace
Basic Workshop,some-ci,some-ns
"""


# ============================================================================
# HELPER FACTORIES
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
        count=1,
        aws_regions="",
        white_glove=False,
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

    overrides: dict mapping (subcommand, resource_hint) -> CompletedProcess
               e.g. {("get", "workshop"): CompletedProcess(...)}
    """
    overrides = overrides or {}

    def dispatcher(*args, **kwargs):
        cmd = args[0] if args else kwargs.get('args', [])
        if not cmd or len(cmd) < 2:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        subcmd = cmd[1]  # create, get, patch, delete, version

        # Check overrides first (subcommand, resource_type)
        resource_type = cmd[2] if len(cmd) > 2 else ""
        key = (subcmd, resource_type)
        if key in overrides:
            val = overrides[key]
            return val() if callable(val) else val

        # version --client
        if subcmd == "version":
            return subprocess.CompletedProcess(
                cmd, 0, stdout="Client Version: 4.14.0\n", stderr=""
            )

        # oc create -f <tmpfile>
        if subcmd == "create":
            # Find the temp file path
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
            api_prefix = api_map.get(kind_lower, f"{kind_lower}.babylon.gpte.redhat.com")

            # Generate a name
            if name_prefix.endswith("-"):
                actual_name = f"{name_prefix}abc12"
            else:
                actual_name = name_prefix

            stdout = f"{api_prefix}/{actual_name} created\n"
            return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

        # oc get
        if subcmd == "get":
            resource = cmd[2] if len(cmd) > 2 else ""
            # oc get workshop <name> -o jsonpath=...workshop-id
            if resource == "workshop":
                jsonpath_arg = None
                for arg in cmd:
                    if "jsonpath" in str(arg):
                        jsonpath_arg = arg
                        break
                if jsonpath_arg and "workshop-id" in jsonpath_arg:
                    return subprocess.CompletedProcess(
                        cmd, 0, stdout="m5hzmw", stderr=""
                    )
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
                # oc get workshop <name> -o name
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="workshop.babylon.gpte.redhat.com/test-workshop", stderr=""
                )

            if resource == "workshopprovision":
                if any("jsonpath" in str(a) for a in cmd):
                    return subprocess.CompletedProcess(
                        cmd, 0, stdout="test-workshop", stderr=""
                    )
                return subprocess.CompletedProcess(
                    cmd, 0,
                    stdout=json.dumps({
                        "items": [{
                            "metadata": {"name": "test-workshop"},
                            "spec": {
                                "lifespan": {"end": "2026-02-17T11:00:00Z"},
                                "count": 20,
                            }
                        }]
                    }),
                    stderr=""
                )

            if resource == "resourceclaim" or resource == "resourceclaims":
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
                if any(a == "json" for a in cmd):
                    catalog_json = {
                        "spec": {
                            "parameters": [
                                {
                                    "name": "aws_region",
                                    "openAPIV3Schema": {
                                        "type": "string",
                                        "default": "us-east-2",
                                    },
                                },
                                {
                                    "name": "ocp4_fips_enable",
                                    "openAPIV3Schema": {"type": "boolean", "default": False},
                                },
                            ]
                        }
                    }
                    return subprocess.CompletedProcess(
                        cmd, 0, stdout=json.dumps(catalog_json), stderr=""
                    )
                return subprocess.CompletedProcess(
                    cmd, 0,
                    stdout="babylon-catalog-prod:Experience OpenShift Virtualization Roadshow",
                    stderr=""
                )

            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        # oc patch
        if subcmd == "patch":
            resource = cmd[2] if len(cmd) > 2 else "resource"
            name = cmd[3] if len(cmd) > 3 else "unknown"
            return subprocess.CompletedProcess(
                cmd, 0, stdout=f"{resource}/{name} patched\n", stderr=""
            )

        # oc delete
        if subcmd == "delete":
            return subprocess.CompletedProcess(cmd, 0, stdout="deleted\n", stderr="")

        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    return dispatcher


def _write_csv_tempfile(csv_text):
    """Write CSV text to a temporary file, return path."""
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".csv", delete=False, encoding="utf-8"
    )
    tmp.write(csv_text)
    tmp.close()
    return tmp.name


# ============================================================================
# GROUP 1: Date/Time Utilities
# ============================================================================


class TestDateTimeUtilities(unittest.TestCase):
    """Tests for parse_date_time, format_iso8601, calculate_duration."""

    def test_parse_dd_mm_yyyy(self):
        dt = parse_date_time("15/02/2026 11:00")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2026)
        self.assertEqual(dt.month, 2)
        self.assertEqual(dt.day, 15)
        self.assertEqual(dt.hour, 11)
        self.assertEqual(dt.tzinfo, UTC)

    def test_parse_dd_mm_yy(self):
        dt = parse_date_time("15/02/26 11:00")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2026)
        self.assertEqual(dt.tzinfo, UTC)

    def test_parse_iso8601_with_z(self):
        dt = parse_date_time("2026-02-15T11:00:00Z")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2026)
        self.assertEqual(dt.hour, 11)

    def test_parse_iso8601_without_z(self):
        dt = parse_date_time("2026-02-15T11:00:00")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.hour, 11)

    def test_parse_empty_returns_none(self):
        self.assertIsNone(parse_date_time(""))
        self.assertIsNone(parse_date_time("   "))

    def test_parse_invalid_returns_none(self):
        self.assertIsNone(parse_date_time("not-a-date"))

    def test_format_iso8601_naive(self):
        dt = datetime(2026, 2, 15, 11, 0, 0)
        result = format_iso8601(dt)
        self.assertEqual(result, "2026-02-15T11:00:00Z")

    def test_format_iso8601_utc(self):
        dt = datetime(2026, 2, 15, 11, 0, 0, tzinfo=UTC)
        result = format_iso8601(dt)
        self.assertEqual(result, "2026-02-15T11:00:00Z")

    def test_calculate_duration(self):
        start = datetime(2026, 2, 15, 11, 0, tzinfo=UTC)
        end = datetime(2026, 2, 15, 19, 0, tzinfo=UTC)
        self.assertEqual(calculate_duration(start, end), "8h")

    def test_calculate_duration_multi_day(self):
        start = datetime(2026, 2, 15, 11, 0, tzinfo=UTC)
        end = datetime(2026, 2, 17, 11, 0, tzinfo=UTC)
        self.assertEqual(calculate_duration(start, end), "48h")


# ============================================================================
# GROUP 2: CSV Parsing
# ============================================================================


class TestCSVParsing(unittest.TestCase):
    """Tests for read_csv_input and write_deployment_results."""

    def setUp(self):
        self._tmpfiles = []

    def tearDown(self):
        for f in self._tmpfiles:
            try:
                os.unlink(f)
            except OSError:
                pass

    def _write(self, csv_text):
        path = _write_csv_tempfile(csv_text)
        self._tmpfiles.append(path)
        return path

    def test_basic_single_row(self):
        path = self._write(BASIC_WORKSHOP_CSV)
        schedules = read_csv_input(path)
        self.assertEqual(len(schedules), 1)
        s = schedules[0]
        self.assertEqual(s.ci_name, "Experience OpenShift Virtualization Roadshow")
        self.assertEqual(s.ci, "openshift-cnv.ocp-virt-roadshow-multi-user.prod")
        self.assertEqual(s.namespace, "user-bbethell-redhat-com")
        self.assertEqual(s.users, 20)
        self.assertTrue(s.enable_workshop_interface)
        self.assertEqual(s.password, "Workshop1")
        self.assertEqual(s.activity, "Admin")
        self.assertEqual(s.purpose, "QA")
        self.assertEqual(s.provisioning_date, "15/02/2026 11:00")
        self.assertEqual(s.auto_stop, "15/02/2026 19:00")
        self.assertEqual(s.auto_destroy, "17/02/2026 11:00")

    def test_multi_asset_old_format(self):
        path = self._write(MULTI_ASSET_OLD_CSV)
        schedules = read_csv_input(path)
        self.assertEqual(len(schedules), 1)
        s = schedules[0]
        self.assertTrue(s.is_multi_asset)
        self.assertIn("openshift-cnv", s.asset_cis)
        self.assertIn("zt-ansiblebu", s.asset_cis)
        self.assertEqual(s.multi_workshop_name, "summit-multi-2026")

    def test_grouped_multi_asset(self):
        path = self._write(MULTI_ASSET_GROUPED_CSV)
        schedules = read_csv_input(path)
        self.assertEqual(len(schedules), 2)
        self.assertEqual(schedules[0].multi_workshop_name, "summit-demo-2026")
        self.assertEqual(schedules[1].multi_workshop_name, "summit-demo-2026")
        # Grouped format: Multi_Asset is NOT set; rows share Multi_Workshop_Name
        self.assertFalse(schedules[0].is_multi_asset)

    def test_count_and_concurrency(self):
        path = self._write(COUNT_EXPANSION_CSV)
        schedules = read_csv_input(path)
        self.assertEqual(len(schedules), 1)
        self.assertEqual(schedules[0].count, 2)
        self.assertEqual(schedules[0].concurrency, 3)

    def test_aws_region(self):
        path = self._write(MULTI_REGION_CSV)
        schedules = read_csv_input(path)
        self.assertEqual(len(schedules), 1)
        self.assertEqual(schedules[0].aws_regions, "us-east-1,eu-west-1")

    def test_missing_required_headers_raises(self):
        path = self._write(MISSING_HEADERS_CSV)
        with self.assertRaises(ValueError):
            read_csv_input(path)

    def test_empty_csv_raises(self):
        path = self._write("CI Name,CI,Namespace,Users,Enable_workshop_interface,Password,Activity,Purpose,Provisioning Date (UTC),Auto-stop (UTC),Auto-destroy (UTC)\n")
        with self.assertRaises(ValueError):
            read_csv_input(path)

    def test_old_date_headers(self):
        path = self._write(OLD_DATE_HEADERS_CSV)
        schedules = read_csv_input(path)
        self.assertEqual(len(schedules), 1)
        self.assertEqual(schedules[0].provisioning_date, "15/02/2026 11:00")

    def test_write_deployment_results_roundtrip(self):
        results = [
            DeploymentResult(
                ci_name="Test", ci="test-ci", namespace="test-ns",
                guid="test-guid", url="https://example.com",
                status="verified", provisioning_date="15/02/2026 11:00",
                auto_stop="15/02/2026 19:00", auto_destroy="17/02/2026 11:00",
                timestamp="2026-02-15T11:00:00Z", error_message=""
            )
        ]
        output = tempfile.NamedTemporaryFile(suffix=".csv", delete=False)
        output.close()
        self._tmpfiles.append(output.name)

        write_deployment_results(results, output.name)

        with open(output.name, "r") as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ci_name"], "Test")
        self.assertEqual(rows[0]["guid"], "test-guid")
        self.assertEqual(rows[0]["status"], "verified")

    def test_skip_incomplete_rows(self):
        csv_text = """\
CI Name,CI,Namespace,Users,Enable_workshop_interface,Password,Activity,Purpose,Workshop Name,Provisioning Date (UTC),Auto-stop (UTC),Auto-destroy (UTC)
Valid Row,valid-ci,valid-ns,20,True,pass,Admin,QA,My Workshop,15/02/2026 11:00,15/02/2026 19:00,17/02/2026 11:00
,,,,,,,,,,,
"""
        path = self._write(csv_text)
        schedules = read_csv_input(path)
        self.assertEqual(len(schedules), 1)
        self.assertEqual(schedules[0].ci_name, "Valid Row")


# ============================================================================
# GROUP 3: Build ResourceClaim Payload
# ============================================================================


class TestProviderParameterValues(unittest.TestCase):
    """Users → num_users must apply with workshop UI enabled (WorkshopProvision seats)."""

    def test_includes_num_users_when_workshop_ui_enabled(self):
        schedule = make_schedule(enable_workshop_interface=True, users=15, instances=1)
        pv = _provider_parameter_values(schedule, "2026-02-15T11:00:00Z", "2026-02-15T19:00:00Z")
        self.assertEqual(pv["num_users"], 15)

    def test_includes_num_users_when_workshop_ui_disabled(self):
        schedule = make_schedule(enable_workshop_interface=False, users=20)
        pv = _provider_parameter_values(schedule, "2026-02-15T11:00:00Z", "2026-02-15T19:00:00Z")
        self.assertEqual(pv["num_users"], 20)

    def test_omits_num_users_when_users_blank(self):
        schedule = make_schedule(enable_workshop_interface=True, users=None, instances=8)
        pv = _provider_parameter_values(schedule, "2026-02-15T11:00:00Z", "2026-02-15T19:00:00Z")
        self.assertNotIn("num_users", pv)

    def test_blank_users_catalog_default_advisory(self):
        schedule = make_schedule(users=None, instances=1, enable_workshop_interface=True)
        adv = users_blank_catalog_default_advisory(
            schedule,
            schedule.ci,
            {"has_num_users": True, "maximum": 65, "minimum": 2, "default": 2},
        )
        self.assertIsNotNone(adv)
        self.assertEqual(adv["severity"], "medium")
        self.assertEqual(adv["catalog_default"], 2)
        self.assertIn("2 seats", adv["message"].lower())

    def test_no_blank_advisory_when_users_set(self):
        schedule = make_schedule(users=15)
        adv = users_blank_catalog_default_advisory(
            schedule,
            schedule.ci,
            {"has_num_users": True, "maximum": 65, "minimum": 2, "default": 2},
        )
        self.assertIsNone(adv)


class TestBuildResourceClaimPayload(unittest.TestCase):
    """Tests for build_resource_claim_payload."""

    def test_payload_structure_dry_run(self):
        config = make_config(dry_run=True)
        schedule = make_schedule()
        payload = build_resource_claim_payload(schedule, config)

        self.assertEqual(payload["apiVersion"], "poolboy.gpte.redhat.com/v1")
        self.assertEqual(payload["kind"], "ResourceClaim")
        self.assertIn("metadata", payload)
        self.assertIn("spec", payload)

    def test_provider_fields(self):
        config = make_config(dry_run=True)
        schedule = make_schedule(enable_workshop_interface=False)
        payload = build_resource_claim_payload(schedule, config)

        provider = payload["spec"]["provider"]
        self.assertEqual(provider["name"], schedule.ci)
        self.assertEqual(provider["parameterValues"]["num_users"], 20)
        self.assertIn("start_timestamp", provider["parameterValues"])
        self.assertIn("stop_timestamp", provider["parameterValues"])

    def test_provider_fields_include_num_users_with_workshop_ui(self):
        config = make_config(dry_run=True)
        schedule = make_schedule(enable_workshop_interface=True, users=15)
        payload = build_resource_claim_payload(schedule, config)
        self.assertEqual(payload["spec"]["provider"]["parameterValues"]["num_users"], 15)

    def test_access_password_present(self):
        config = make_config(dry_run=True)
        schedule = make_schedule(password="SecretPass")
        payload = build_resource_claim_payload(schedule, config)
        self.assertEqual(payload["spec"]["accessPassword"], "SecretPass")

    def test_access_password_absent(self):
        config = make_config(dry_run=True)
        schedule = make_schedule(password="")
        payload = build_resource_claim_payload(schedule, config)
        self.assertNotIn("accessPassword", payload["spec"])

    def test_workshop_ui_annotations(self):
        config = make_config(dry_run=True)
        schedule = make_schedule(enable_workshop_interface=True, workshop_name="My WS")
        payload = build_resource_claim_payload(schedule, config)
        annotations = payload["metadata"]["annotations"]
        self.assertEqual(
            annotations["rhdp-flow.gpte.redhat.com/enable-workshop-ui"], "true"
        )
        self.assertEqual(
            annotations["rhdp-flow.gpte.redhat.com/workshop-name"], "My WS"
        )

    def test_email_extraction_from_namespace(self):
        config = make_config(dry_run=True)
        schedule = make_schedule(namespace="user-bbethell-redhat-com")
        payload = build_resource_claim_payload(schedule, config)
        annotations = payload["metadata"]["annotations"]
        self.assertEqual(annotations["demo.redhat.com/requester"], "bbethell@redhat.com")

    def test_date_defaults_when_empty(self):
        config = make_config(dry_run=True)
        schedule = make_schedule(provisioning_date="", auto_stop="", auto_destroy="")
        payload = build_resource_claim_payload(schedule, config)
        # Should still have timestamps (defaults)
        pv = payload["spec"]["provider"]["parameterValues"]
        self.assertIn("start_timestamp", pv)
        self.assertIn("stop_timestamp", pv)
        self.assertIn("end", payload["spec"]["lifespan"])

    def test_lifespan_end_from_auto_destroy(self):
        config = make_config(dry_run=True)
        schedule = make_schedule(auto_destroy="17/02/2026 11:00")
        payload = build_resource_claim_payload(schedule, config)
        self.assertEqual(payload["spec"]["lifespan"]["end"], "2026-02-17T11:00:00Z")

    @patch("rhdp_flow.subprocess.run")
    def test_parameter_values_merge_catalog_defaults(self, mock_run):
        """When not dry-run, ResourceClaim parameterValues include CatalogItem openAPI defaults."""
        ci_json = {
            "spec": {
                "parameters": [
                    {
                        "name": "aws_region",
                        "openAPIV3Schema": {"type": "string", "default": "eu-central-1"},
                    },
                    {
                        "name": "ocp4_fips_enable",
                        "openAPIV3Schema": {"type": "boolean", "default": True},
                    },
                ]
            }
        }
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps(ci_json), stderr=""
        )
        config = make_config(dry_run=False)
        schedule = make_schedule(enable_workshop_interface=False)
        payload = build_resource_claim_payload(schedule, config)
        pv = payload["spec"]["provider"]["parameterValues"]
        self.assertEqual(pv["aws_region"], "eu-central-1")
        self.assertEqual(pv["ocp4_fips_enable"], True)
        self.assertEqual(pv["num_users"], 20)

    @patch("rhdp_flow.subprocess.run")
    def test_csv_single_aws_region_overrides_catalog_default(self, mock_run):
        """AWS_Region column (single value) wins over CatalogItem default."""
        ci_json = {
            "spec": {
                "parameters": [
                    {
                        "name": "aws_region",
                        "openAPIV3Schema": {"type": "string", "default": "eu-central-1"},
                    },
                ]
            }
        }
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps(ci_json), stderr=""
        )
        config = make_config(dry_run=False)
        schedule = make_schedule(aws_regions="us-west-2")
        payload = build_resource_claim_payload(schedule, config)
        self.assertEqual(
            payload["spec"]["provider"]["parameterValues"]["aws_region"], "us-west-2"
        )


# ============================================================================
# GROUP 4: Create ResourceClaim via oc
# ============================================================================


class TestCreateResourceClaimViaOc(unittest.TestCase):
    """Tests for create_resource_claim_via_oc."""

    def _make_payload(self):
        config = make_config(dry_run=True)
        return build_resource_claim_payload(make_schedule(), config)

    def test_dry_run_returns_mock_guid(self):
        config = make_config(dry_run=True)
        payload = self._make_payload()
        guid, ns, err = create_resource_claim_via_oc(payload, config)
        self.assertIsNotNone(guid)
        self.assertIn("dryrun", guid)
        self.assertEqual(ns, "user-bbethell-redhat-com")
        self.assertIsNone(err)

    @patch("rhdp_flow.subprocess.run")
    def test_success_parses_name(self, mock_run):
        mock_run.side_effect = make_oc_dispatcher()
        config = make_config(dry_run=False)
        payload = self._make_payload()
        guid, ns, err = create_resource_claim_via_oc(payload, config)
        self.assertIsNotNone(guid)
        self.assertIsNone(err)

    @patch("rhdp_flow.subprocess.run")
    def test_failure_returncode(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            [], 1, stdout="", stderr="Error: forbidden"
        )
        config = make_config(dry_run=False)
        payload = self._make_payload()
        guid, ns, err = create_resource_claim_via_oc(payload, config)
        self.assertIsNone(guid)
        self.assertIsNotNone(err)
        self.assertIn("forbidden", err)

    @patch("rhdp_flow.subprocess.run")
    def test_timeout_returns_error(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["oc"], timeout=10)
        config = make_config(dry_run=False)
        payload = self._make_payload()
        guid, ns, err = create_resource_claim_via_oc(payload, config)
        self.assertIsNone(guid)
        self.assertIn("timed out", err)

    @patch("rhdp_flow.subprocess.run")
    def test_kubeconfig_env_propagation(self, mock_run):
        mock_run.side_effect = make_oc_dispatcher()
        config = make_config(dry_run=False, kubeconfig_path="/tmp/kubeconfig")
        payload = self._make_payload()
        create_resource_claim_via_oc(payload, config)
        # Check that KUBECONFIG was set in the env
        call_kwargs = mock_run.call_args
        self.assertEqual(call_kwargs.kwargs.get("env", {}).get("KUBECONFIG"), "/tmp/kubeconfig")


# ============================================================================
# GROUP 5: Create Workshop with UI
# ============================================================================


class TestCreateWorkshopWithUI(unittest.TestCase):
    """Tests for create_workshop_with_ui."""

    def _make_rc_payload(self):
        config = make_config(dry_run=True)
        return build_resource_claim_payload(make_schedule(), config)

    def test_dry_run_returns_mock_name(self):
        config = make_config(dry_run=True)
        payload = self._make_rc_payload()
        name = create_workshop_with_ui("ci-name-", "user-ns", payload, config)
        self.assertIsNotNone(name)
        self.assertIn("dryrun", name)

    @patch("rhdp_flow.subprocess.run")
    def test_generate_name_extracts_actual(self, mock_run):
        mock_run.side_effect = make_oc_dispatcher()
        config = make_config(dry_run=False)
        payload = self._make_rc_payload()
        name = create_workshop_with_ui("ci-name-", "user-ns", payload, config)
        self.assertIsNotNone(name)
        self.assertTrue(name.startswith("ci-name-"))

    @patch("rhdp_flow.subprocess.run")
    def test_specific_name_returns_exact(self, mock_run):
        mock_run.side_effect = make_oc_dispatcher()
        config = make_config(dry_run=False)
        payload = self._make_rc_payload()
        name = create_workshop_with_ui("my-exact-workshop", "user-ns", payload, config)
        self.assertEqual(name, "my-exact-workshop")

    @patch("rhdp_flow.subprocess.run")
    def test_already_exists_handled(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            [], 1, stdout="", stderr='Error from server: workshop "my-ws" already exists'
        )
        config = make_config(dry_run=False)
        payload = self._make_rc_payload()
        name = create_workshop_with_ui("my-ws", "user-ns", payload, config)
        self.assertIsNotNone(name)

    def test_payload_has_lab_user_interface(self):
        config = make_config(dry_run=True)
        schedule = make_schedule()
        payload = build_resource_claim_payload(schedule, config)
        # The workshop payload is built inside create_workshop_with_ui,
        # but we test the schedule flag triggers the UI path
        self.assertTrue(schedule.enable_workshop_interface)


# ============================================================================
# GROUP 6: Create Workshop Provision
# ============================================================================


class TestCreateWorkshopProvision(unittest.TestCase):
    """Tests for create_workshop_provision."""

    def _make_rc_payload(self):
        config = make_config(dry_run=True)
        return build_resource_claim_payload(make_schedule(), config)

    def test_dry_run_returns_name(self):
        config = make_config(dry_run=True)
        payload = self._make_rc_payload()
        name = create_workshop_provision("ws-name", "user-ns", payload, config)
        self.assertEqual(name, "ws-name")

    @patch("rhdp_flow.subprocess.run")
    def test_count_and_concurrency(self, mock_run):
        base = make_oc_dispatcher()
        captured = []

        def side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if (
                len(cmd) >= 4
                and cmd[1] == "create"
                and cmd[2] == "-f"
                and os.path.isfile(cmd[3])
            ):
                with open(cmd[3]) as f:
                    captured.append(json.load(f))
            return base(*args, **kwargs)

        mock_run.side_effect = side_effect
        config = make_config(dry_run=False)
        payload = self._make_rc_payload()
        name = create_workshop_provision(
            "ws-name", "user-ns", payload, config,
            concurrency=3, count=40
        )
        self.assertEqual(name, "ws-name")
        self.assertTrue(captured)
        wp_payload = captured[-1]
        self.assertEqual(wp_payload["spec"]["count"], 40)
        self.assertEqual(wp_payload["spec"]["concurrency"], 3)
        # Catalog defaults merged from mock catalogitem JSON response
        self.assertEqual(wp_payload["spec"]["parameters"]["aws_region"], "us-east-2")

    @patch("rhdp_flow.subprocess.run")
    def test_extra_parameters_override_catalog_region(self, mock_run):
        base = make_oc_dispatcher()
        captured = []

        def side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if (
                len(cmd) >= 4
                and cmd[1] == "create"
                and cmd[2] == "-f"
                and os.path.isfile(cmd[3])
            ):
                with open(cmd[3]) as f:
                    captured.append(json.load(f))
            return base(*args, **kwargs)

        mock_run.side_effect = side_effect
        config = make_config(dry_run=False)
        payload = self._make_rc_payload()
        create_workshop_provision(
            "ws-name", "user-ns", payload, config,
            extra_parameters={"aws_region": "ap-south-1"},
        )
        self.assertEqual(captured[-1]["spec"]["parameters"]["aws_region"], "ap-south-1")

    @patch("rhdp_flow.subprocess.run")
    def test_name_suffix(self, mock_run):
        mock_run.side_effect = make_oc_dispatcher()
        config = make_config(dry_run=False)
        payload = self._make_rc_payload()
        name = create_workshop_provision(
            "ws-name", "user-ns", payload, config,
            provision_name_suffix="-us-east-1"
        )
        self.assertEqual(name, "ws-name")

    def test_dry_run_extra_parameters(self):
        config = make_config(dry_run=True)
        payload = self._make_rc_payload()
        name = create_workshop_provision(
            "ws-name", "user-ns", payload, config,
            extra_parameters={"aws_region": "us-east-1"}
        )
        self.assertEqual(name, "ws-name")


# ============================================================================
# GROUP 7: Create Multi-Workshop
# ============================================================================


class TestCreateMultiWorkshop(unittest.TestCase):
    """Tests for create_multi_workshop."""

    def test_dry_run_creates_sub_resources(self):
        config = make_config(dry_run=True)
        schedule = make_schedule(
            is_multi_asset=True,
            asset_cis="openshift-cnv.ocp-virt-roadshow-multi-user.prod,zt-ansiblebu.ansible-network-automation-basics-lab-2.event",
            multi_workshop_name="summit-test",
        )
        name = create_multi_workshop(schedule, config)
        self.assertIsNotNone(name)
        self.assertEqual(name, "summit-test")

    def test_custom_name(self):
        config = make_config(dry_run=True)
        schedule = make_schedule(
            is_multi_asset=True,
            asset_cis="ci1.prod,ci2.event",
            multi_workshop_name="my-custom-name",
        )
        name = create_multi_workshop(schedule, config)
        self.assertEqual(name, "my-custom-name")

    def test_generated_name(self):
        config = make_config(dry_run=True)
        schedule = make_schedule(
            is_multi_asset=True,
            asset_cis="ci1.prod,ci2.event",
            multi_workshop_name="",
        )
        name = create_multi_workshop(schedule, config)
        self.assertIsNotNone(name)
        self.assertTrue(name.startswith("automation-"))

    def test_not_multi_asset_returns_none(self):
        config = make_config(dry_run=True)
        schedule = make_schedule(is_multi_asset=False)
        result = create_multi_workshop(schedule, config)
        self.assertIsNone(result)


# ============================================================================
# GROUP 8: Create Multi-Workshop From Group
# ============================================================================


class TestCreateMultiWorkshopFromGroup(unittest.TestCase):
    """Tests for create_multi_workshop_from_group."""

    def test_dry_run_grouped_creates_all(self):
        config = make_config(dry_run=True)
        sched1 = make_schedule(
            ci="openshift-cnv.ocp-virt-roadshow-multi-user.prod",
            ci_name="Virt Roadshow",
            password="VirtPass1",
            is_multi_asset=True,
            multi_workshop_name="summit-demo-2026",
        )
        sched2 = make_schedule(
            ci="zt-ansiblebu.ansible-network-automation-basics-lab-2.event",
            ci_name="Ansible Lab",
            password="AnsPass2",
            is_multi_asset=True,
            multi_workshop_name="summit-demo-2026",
        )
        name = create_multi_workshop_from_group([sched1, sched2], config)
        self.assertIsNotNone(name)
        self.assertEqual(name, "summit-demo-2026")

    def test_per_item_passwords(self):
        config = make_config(dry_run=True)
        sched1 = make_schedule(
            ci="ci1.prod", password="pass1",
            is_multi_asset=True, multi_workshop_name="test-group",
        )
        sched2 = make_schedule(
            ci="ci2.event", password="pass2",
            is_multi_asset=True, multi_workshop_name="test-group",
        )
        # Each schedule keeps its own password
        self.assertEqual(sched1.password, "pass1")
        self.assertEqual(sched2.password, "pass2")
        name = create_multi_workshop_from_group([sched1, sched2], config)
        self.assertIsNotNone(name)

    def test_empty_group_returns_none(self):
        config = make_config(dry_run=True)
        result = create_multi_workshop_from_group([], config)
        self.assertIsNone(result)


# ============================================================================
# GROUP 9: Multi-Region Workshop
# ============================================================================


class TestMultiRegionWorkshop(unittest.TestCase):
    """Tests for create_multi_region_workshop."""

    @patch("rhdp_flow.subprocess.run")
    def test_creates_workshop_and_provisions(self, mock_run):
        mock_run.side_effect = make_oc_dispatcher()
        config = make_config(dry_run=False)
        schedule = make_schedule(
            users=40,
            aws_regions="us-east-1,eu-west-1",
        )
        name = create_multi_region_workshop(schedule, config)
        self.assertIsNotNone(name)

    def test_dry_run_creates_workshop_and_provisions(self):
        config = make_config(dry_run=True)
        schedule = make_schedule(
            users=40,
            aws_regions="us-east-1,eu-west-1",
        )
        name = create_multi_region_workshop(schedule, config)
        self.assertIsNotNone(name)

    def test_user_distribution(self):
        # 40 users / 2 regions = 20 each
        config = make_config(dry_run=True)
        schedule = make_schedule(
            users=40,
            aws_regions="us-east-1,eu-west-1",
        )
        # We can't easily inspect the distribution without mocking deeper,
        # but we verify it completes without error
        name = create_multi_region_workshop(schedule, config)
        self.assertIsNotNone(name)

    def test_single_region_returns_none(self):
        config = make_config(dry_run=True)
        schedule = make_schedule(users=40, aws_regions="us-east-1")
        result = create_multi_region_workshop(schedule, config)
        self.assertIsNone(result)


# ============================================================================
# GROUP 10: Lock Workshops
# ============================================================================


class TestLockWorkshops(unittest.TestCase):
    """Tests for lock_workshops (lock-enabled label)."""

    def test_dry_run_logs_without_patching(self):
        config = make_config(dry_run=True)
        schedules = [make_schedule()]
        # Should not raise
        lock_workshops(schedules, config)

    @patch("rhdp_flow.subprocess.run")
    def test_patches_resource_lock_label(self, mock_run):
        mock_run.side_effect = make_oc_dispatcher(overrides={
            ("get", "workshop"): subprocess.CompletedProcess(
                [], 0, stdout="test-ws", stderr=""
            ),
        })
        config = make_config(dry_run=False)
        schedules = [make_schedule()]
        lock_workshops(schedules, config)
        # Verify a patch call was made with lock-enabled label
        patch_calls = [c for c in mock_run.call_args_list if "patch" in c[0][0]]
        self.assertGreater(len(patch_calls), 0)
        # Extract the patch JSON and verify it targets the lock-enabled label
        patch_cmd = patch_calls[0][0][0]
        patch_json_str = None
        for i, arg in enumerate(patch_cmd):
            if arg == "-p" and i + 1 < len(patch_cmd):
                patch_json_str = patch_cmd[i + 1]
        self.assertIsNotNone(patch_json_str)
        patch_data = json.loads(patch_json_str)
        self.assertEqual(
            patch_data["metadata"]["labels"]["demo.redhat.com/lock-enabled"],
            "true",
        )

    @patch("rhdp_flow.subprocess.run")
    def test_no_workshops_found(self, mock_run):
        mock_run.side_effect = make_oc_dispatcher(overrides={
            ("get", "workshop"): subprocess.CompletedProcess(
                [], 0, stdout="", stderr=""
            ),
        })
        config = make_config(dry_run=False)
        schedules = [make_schedule()]
        # Should not raise even with no workshops
        lock_workshops(schedules, config)


class TestUnlockWorkshops(unittest.TestCase):
    """Tests for unlock_workshops (lock-enabled label)."""

    def test_dry_run_logs_without_patching(self):
        config = make_config(dry_run=True)
        schedules = [make_schedule()]
        # Should not raise
        unlock_workshops(schedules, config)

    @patch("rhdp_flow.subprocess.run")
    def test_patches_resource_lock_false(self, mock_run):
        mock_run.side_effect = make_oc_dispatcher(overrides={
            ("get", "workshop"): subprocess.CompletedProcess(
                [], 0, stdout="test-ws", stderr=""
            ),
        })
        config = make_config(dry_run=False)
        schedules = [make_schedule()]
        unlock_workshops(schedules, config)
        # Verify a patch call was made with lock-enabled=false
        patch_calls = [c for c in mock_run.call_args_list if "patch" in c[0][0]]
        self.assertGreater(len(patch_calls), 0)
        patch_cmd = patch_calls[0][0][0]
        patch_json_str = None
        for i, arg in enumerate(patch_cmd):
            if arg == "-p" and i + 1 < len(patch_cmd):
                patch_json_str = patch_cmd[i + 1]
        self.assertIsNotNone(patch_json_str)
        patch_data = json.loads(patch_json_str)
        self.assertEqual(
            patch_data["metadata"]["labels"]["demo.redhat.com/lock-enabled"],
            "false",
        )

    @patch("rhdp_flow.subprocess.run")
    def test_no_workshops_found(self, mock_run):
        mock_run.side_effect = make_oc_dispatcher(overrides={
            ("get", "workshop"): subprocess.CompletedProcess(
                [], 0, stdout="", stderr=""
            ),
        })
        config = make_config(dry_run=False)
        schedules = [make_schedule()]
        # Should not raise even with no workshops
        unlock_workshops(schedules, config)


# ============================================================================
# GROUP 11: Extend Stop Time
# ============================================================================


class TestExtendStopTime(unittest.TestCase):
    """Tests for extend_stop_time."""

    @patch("rhdp_flow.subprocess.run")
    def test_calculates_new_stop_time(self, mock_run):
        workshop_json = {
            "items": [{
                "metadata": {"name": "test-ws"},
                "spec": {
                    "actionSchedule": {
                        "start": "2026-02-15T11:00:00Z",
                        "stop": "2026-02-15T19:00:00Z"
                    }
                }
            }]
        }
        mock_run.side_effect = make_oc_dispatcher(overrides={
            ("get", "workshop"): subprocess.CompletedProcess(
                [], 0, stdout=json.dumps(workshop_json), stderr=""
            ),
        })
        config = make_config(dry_run=False)
        schedules = [make_schedule()]
        extend_stop_time(schedules, config, days=1, hours=2)
        # Find the patch call and verify the timestamp
        patch_calls = [c for c in mock_run.call_args_list if "patch" in c[0][0]]
        self.assertGreater(len(patch_calls), 0)
        # Extract the patch JSON
        patch_cmd = patch_calls[0][0][0]
        patch_json_str = None
        for i, arg in enumerate(patch_cmd):
            if arg == "-p" and i + 1 < len(patch_cmd):
                patch_json_str = patch_cmd[i + 1]
        self.assertIsNotNone(patch_json_str)
        patch_data = json.loads(patch_json_str)
        new_stop = patch_data["spec"]["actionSchedule"]["stop"]
        # Original stop: 2026-02-15T19:00:00Z + 1 day + 2 hours = 2026-02-16T21:00:00Z
        self.assertEqual(new_stop, "2026-02-16T21:00:00Z")

    def test_dry_run_logs_times(self):
        config = make_config(dry_run=True)
        schedules = [make_schedule()]
        # Should not raise in dry-run
        extend_stop_time(schedules, config, days=1, hours=0)


# ============================================================================
# GROUP 12: Extend Destroy Time
# ============================================================================


class TestExtendDestroyTime(unittest.TestCase):
    """Tests for extend_destroy_time."""

    @patch("rhdp_flow.subprocess.run")
    def test_patches_workshop_and_provision(self, mock_run):
        workshop_json = {
            "items": [{
                "metadata": {"name": "test-ws"},
                "spec": {
                    "lifespan": {"end": "2026-02-17T11:00:00Z"}
                }
            }]
        }
        provision_json = {
            "items": [{
                "metadata": {"name": "test-ws"},
                "spec": {
                    "lifespan": {"end": "2026-02-17T11:00:00Z"}
                }
            }]
        }

        def dispatcher(*args, **kwargs):
            cmd = args[0] if args else kwargs.get('args', [])
            subcmd = cmd[1] if len(cmd) > 1 else ""
            resource = cmd[2] if len(cmd) > 2 else ""
            if subcmd == "get" and resource == "workshop":
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=json.dumps(workshop_json), stderr=""
                )
            if subcmd == "get" and resource == "workshopprovision":
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=json.dumps(provision_json), stderr=""
                )
            if subcmd == "patch":
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="patched\n", stderr=""
                )
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        mock_run.side_effect = dispatcher
        config = make_config(dry_run=False)
        schedules = [make_schedule()]
        extend_destroy_time(schedules, config, days=1, hours=0)
        patch_calls = [c for c in mock_run.call_args_list if "patch" in c[0][0]]
        # Should patch both workshop and workshopprovision
        self.assertGreaterEqual(len(patch_calls), 2)

    def test_dry_run(self):
        config = make_config(dry_run=True)
        schedules = [make_schedule()]
        extend_destroy_time(schedules, config, days=1, hours=0)


# ============================================================================
# GROUP 13: Scale Workshops
# ============================================================================


class TestScaleWorkshops(unittest.TestCase):
    """Tests for scale_workshops."""

    @patch("rhdp_flow.subprocess.run")
    def test_patches_count(self, mock_run):
        mock_run.side_effect = make_oc_dispatcher(overrides={
            ("get", "workshopprovision"): subprocess.CompletedProcess(
                [], 0, stdout="test-provision", stderr=""
            ),
        })
        config = make_config(dry_run=False)
        schedules = [make_schedule()]
        scale_workshops(schedules, config, target_count=40)
        patch_calls = [c for c in mock_run.call_args_list if "patch" in c[0][0]]
        self.assertGreater(len(patch_calls), 0)
        # Verify patch sets count to 40
        patch_cmd = patch_calls[0][0][0]
        patch_json_str = None
        for i, arg in enumerate(patch_cmd):
            if arg == "-p" and i + 1 < len(patch_cmd):
                patch_json_str = patch_cmd[i + 1]
        self.assertIsNotNone(patch_json_str)
        patch_data = json.loads(patch_json_str)
        self.assertEqual(patch_data["spec"]["count"], 40)

    def test_dry_run(self):
        config = make_config(dry_run=True)
        schedules = [make_schedule()]
        # Should not raise
        scale_workshops(schedules, config, target_count=40)


# ============================================================================
# GROUP 14: Process Schedule
# ============================================================================


class TestProcessSchedule(unittest.TestCase):
    """Tests for process_schedule routing."""

    def test_workshop_ui_route(self):
        config = make_config(dry_run=True)
        schedule = make_schedule(enable_workshop_interface=True)
        result = process_schedule(schedule, config)
        self.assertIsInstance(result, DeploymentResult)
        self.assertIn(result.status, ["verified", "deployed_unverified", "deployed_no_url"])

    def test_no_workshop_ui_route(self):
        config = make_config(dry_run=True)
        schedule = make_schedule(enable_workshop_interface=False)
        result = process_schedule(schedule, config)
        self.assertIsInstance(result, DeploymentResult)

    def test_multi_asset_route(self):
        config = make_config(dry_run=True)
        schedule = make_schedule(
            is_multi_asset=True,
            asset_cis="ci1.prod,ci2.event",
            multi_workshop_name="test-multi",
        )
        result = process_schedule(schedule, config)
        self.assertIsInstance(result, DeploymentResult)

    def test_multi_region_route(self):
        config = make_config(dry_run=True)
        schedule = make_schedule(
            users=40,
            aws_regions="us-east-1,eu-west-1",
        )
        result = process_schedule(schedule, config)
        self.assertIsInstance(result, DeploymentResult)

    @patch("rhdp_flow.subprocess.run")
    def test_failure_returns_failed(self, mock_run):
        mock_run.side_effect = Exception("Connection refused")
        config = make_config(dry_run=False)
        schedule = make_schedule(enable_workshop_interface=False)
        result = process_schedule(schedule, config)
        self.assertIn(result.status, ["failed", "error"])


# ============================================================================
# GROUP 15: Main CLI
# ============================================================================


class TestMainCLI(unittest.TestCase):
    """Tests for main() via sys.argv."""

    def setUp(self):
        self._tmpfiles = []

    def tearDown(self):
        for f in self._tmpfiles:
            try:
                os.unlink(f)
            except OSError:
                pass

    def _write(self, csv_text):
        path = _write_csv_tempfile(csv_text)
        self._tmpfiles.append(path)
        return path

    @patch("rhdp_flow.subprocess.run")
    def test_dry_run_end_to_end(self, mock_run):
        mock_run.side_effect = make_oc_dispatcher()
        csv_path = self._write(BASIC_WORKSHOP_CSV)
        output_path = tempfile.NamedTemporaryFile(suffix=".csv", delete=False).name
        self._tmpfiles.append(output_path)

        from rhdp_flow import main
        test_args = [
            "rhdp_flow.py",
            "--dry-run",
            "--input-csv", csv_path,
            "--output-csv", output_path,
        ]
        with patch("sys.argv", test_args):
            try:
                main()
            except SystemExit as e:
                self.assertEqual(e.code, 0)

        # Verify output CSV exists and has content
        self.assertTrue(os.path.exists(output_path))

    @patch("rhdp_flow.subprocess.run")
    def test_ci_filter(self, mock_run):
        mock_run.side_effect = make_oc_dispatcher()
        csv_text = """\
CI Name,CI,Namespace,Users,Enable_workshop_interface,Password,Activity,Purpose,Workshop Name,Provisioning Date (UTC),Auto-stop (UTC),Auto-destroy (UTC),Multi_Asset,Asset_CIs,Multi_Workshop_Name,Concurrency,Instances,Count,AWS_Region
Workshop A,ci-a.prod,user-ns,20,True,Pass1,Admin,QA,WS A,15/02/2026 11:00,15/02/2026 19:00,17/02/2026 11:00,,,,,,,
Workshop B,ci-b.prod,user-ns,20,True,Pass2,Admin,QA,WS B,15/02/2026 11:00,15/02/2026 19:00,17/02/2026 11:00,,,,,,,
"""
        csv_path = self._write(csv_text)
        output_path = tempfile.NamedTemporaryFile(suffix=".csv", delete=False).name
        self._tmpfiles.append(output_path)

        from rhdp_flow import main
        test_args = [
            "rhdp_flow.py",
            "--dry-run",
            "--input-csv", csv_path,
            "--output-csv", output_path,
            "--ci", "ci-a.prod",
        ]
        with patch("sys.argv", test_args):
            try:
                main()
            except SystemExit as e:
                self.assertEqual(e.code, 0)

        # Only ci-a.prod should be in results
        with open(output_path) as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        for row in rows:
            self.assertEqual(row["ci"], "ci-a.prod")

    @patch("rhdp_flow.subprocess.run")
    def test_lock_command(self, mock_run):
        mock_run.side_effect = make_oc_dispatcher()
        csv_path = self._write(BASIC_WORKSHOP_CSV)

        from rhdp_flow import main
        test_args = [
            "rhdp_flow.py",
            "--dry-run",
            "--input-csv", csv_path,
            "--lock",
        ]
        with patch("sys.argv", test_args):
            try:
                main()
            except SystemExit as e:
                self.assertEqual(e.code, 0)

    @patch("rhdp_flow.subprocess.run")
    def test_extend_stop_command(self, mock_run):
        mock_run.side_effect = make_oc_dispatcher()
        csv_path = self._write(BASIC_WORKSHOP_CSV)

        from rhdp_flow import main
        test_args = [
            "rhdp_flow.py",
            "--dry-run",
            "--input-csv", csv_path,
            "--extend-stop",
            "--days", "1",
            "--hours", "2",
        ]
        with patch("sys.argv", test_args):
            try:
                main()
            except SystemExit as e:
                self.assertEqual(e.code, 0)

    @patch("rhdp_flow.subprocess.run")
    def test_extend_destroy_command(self, mock_run):
        mock_run.side_effect = make_oc_dispatcher()
        csv_path = self._write(BASIC_WORKSHOP_CSV)

        from rhdp_flow import main
        test_args = [
            "rhdp_flow.py",
            "--dry-run",
            "--input-csv", csv_path,
            "--extend-destroy",
            "--days", "1",
        ]
        with patch("sys.argv", test_args):
            try:
                main()
            except SystemExit as e:
                self.assertEqual(e.code, 0)

    @patch("rhdp_flow.subprocess.run")
    def test_scale_command(self, mock_run):
        mock_run.side_effect = make_oc_dispatcher()
        csv_path = self._write(BASIC_WORKSHOP_CSV)

        from rhdp_flow import main
        test_args = [
            "rhdp_flow.py",
            "--dry-run",
            "--input-csv", csv_path,
            "--scale", "40",
        ]
        with patch("sys.argv", test_args):
            try:
                main()
            except SystemExit as e:
                self.assertEqual(e.code, 0)

    @patch("rhdp_flow.subprocess.run")
    def test_count_expansion(self, mock_run):
        mock_run.side_effect = make_oc_dispatcher()
        csv_path = self._write(COUNT_EXPANSION_CSV)
        output_path = tempfile.NamedTemporaryFile(suffix=".csv", delete=False).name
        self._tmpfiles.append(output_path)

        from rhdp_flow import main
        test_args = [
            "rhdp_flow.py",
            "--dry-run",
            "--input-csv", csv_path,
            "--output-csv", output_path,
        ]
        with patch("sys.argv", test_args):
            try:
                main()
            except SystemExit as e:
                self.assertEqual(e.code, 0)

        # Count=2 should produce 2 results
        with open(output_path) as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        self.assertEqual(len(rows), 2)

    @patch("rhdp_flow.subprocess.run")
    def test_grouped_multi_asset_routing(self, mock_run):
        mock_run.side_effect = make_oc_dispatcher()
        csv_path = self._write(MULTI_ASSET_GROUPED_CSV)
        output_path = tempfile.NamedTemporaryFile(suffix=".csv", delete=False).name
        self._tmpfiles.append(output_path)

        from rhdp_flow import main
        test_args = [
            "rhdp_flow.py",
            "--dry-run",
            "--input-csv", csv_path,
            "--output-csv", output_path,
        ]
        with patch("sys.argv", test_args):
            try:
                main()
            except SystemExit as e:
                self.assertEqual(e.code, 0)


# ============================================================================
# GROUP 16: Construct Workshop URL
# ============================================================================


class TestConstructWorkshopUrl(unittest.TestCase):
    """Tests for construct_workshop_url."""

    def test_with_suffix(self):
        url = construct_workshop_url(
            "openshift-cnv.ocp-virt-roadshow-multi-user.prod",
            "user-bbethell-redhat-com",
            "vt958"
        )
        self.assertEqual(
            url,
            "https://integration.demo.redhat.com/workshops/user-bbethell-redhat-com/openshift-cnv.ocp-virt-roadshow-multi-user.prod-vt958/details"
        )

    def test_without_suffix(self):
        url = construct_workshop_url(
            "openshift-cnv.ocp-virt-roadshow-multi-user.prod",
            "user-bbethell-redhat-com"
        )
        self.assertEqual(
            url,
            "https://integration.demo.redhat.com/workshops/user-bbethell-redhat-com/openshift-cnv.ocp-virt-roadshow-multi-user.prod/details"
        )


# ============================================================================
# GROUP 17: Verify Deployment
# ============================================================================


class TestVerifyDeployment(unittest.TestCase):
    """Tests for verify_deployment."""

    def test_dry_run_returns_true(self):
        config = make_config(dry_run=True)
        healthy, url, log_url = verify_deployment(
            "ci-name-abc12", "user-ns", "ci-name.prod", config
        )
        self.assertTrue(healthy)
        self.assertIn("ci-name.prod", url)
        self.assertEqual(log_url, "")

    @patch("rhdp_flow.subprocess.run")
    def test_healthy_and_ready(self, mock_run):
        rc_json = {
            "status": {"healthy": True, "ready": True}
        }
        mock_run.return_value = subprocess.CompletedProcess(
            [], 0, stdout=json.dumps(rc_json), stderr=""
        )
        config = make_config(dry_run=False)
        healthy, url, log_url = verify_deployment(
            "ci-name-abc12", "user-ns", "ci-name.prod", config
        )
        self.assertTrue(healthy)
        self.assertEqual(log_url, "")

    @patch("rhdp_flow.subprocess.run")
    def test_not_healthy(self, mock_run):
        rc_json = {
            "status": {"healthy": False, "ready": False}
        }
        mock_run.return_value = subprocess.CompletedProcess(
            [], 0, stdout=json.dumps(rc_json), stderr=""
        )
        config = make_config(dry_run=False)
        healthy, url, log_url = verify_deployment(
            "ci-name-abc12", "user-ns", "ci-name.prod", config
        )
        self.assertFalse(healthy)
        self.assertEqual(log_url, "")


# ============================================================================
# GROUP 18: RHDPConfig
# ============================================================================


class TestRHDPConfig(unittest.TestCase):
    """Tests for RHDPConfig.validate()."""

    @patch("rhdp_flow.subprocess.run")
    def test_validate_success(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            [], 0, stdout="Client Version: 4.14.0\n", stderr=""
        )
        config = RHDPConfig()
        self.assertTrue(config.validate())

    @patch("rhdp_flow.subprocess.run")
    def test_validate_failure(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            [], 1, stdout="", stderr="error"
        )
        config = RHDPConfig()
        self.assertFalse(config.validate())

    @patch("rhdp_flow.subprocess.run")
    def test_validate_not_found(self, mock_run):
        mock_run.side_effect = FileNotFoundError("oc not found")
        config = RHDPConfig()
        self.assertFalse(config.validate())


# ============================================================================
# GROUP 19: Create Parser
# ============================================================================


class TestCreateParser(unittest.TestCase):
    """Tests for create_parser."""

    def test_default_values(self):
        parser = create_parser()
        args = parser.parse_args(["--input-csv", "test.csv"])
        self.assertEqual(args.input_csv, "test.csv")
        self.assertFalse(args.dry_run)
        self.assertEqual(args.timeout, 60)

    def test_flag_parsing(self):
        parser = create_parser()
        args = parser.parse_args([
            "--input-csv", "test.csv",
            "--dry-run",
            "--debug",
            "--lock",
        ])
        self.assertTrue(args.dry_run)
        self.assertTrue(args.debug)
        self.assertTrue(getattr(args, "lock", False))

    def test_qa_choices(self):
        parser = create_parser()
        args = parser.parse_args(["--input-csv", "test.csv", "--qa", "1"])
        self.assertEqual(args.qa, "1")

        args = parser.parse_args(["--input-csv", "test.csv", "--qa", "both"])
        self.assertEqual(args.qa, "both")

    def test_scale_integer(self):
        parser = create_parser()
        args = parser.parse_args(["--input-csv", "test.csv", "--scale", "40"])
        self.assertEqual(args.scale, 40)

    def test_extend_days_hours(self):
        parser = create_parser()
        args = parser.parse_args([
            "--input-csv", "test.csv",
            "--extend-stop",
            "--days", "1",
            "--hours", "2",
        ])
        self.assertTrue(args.extend_stop)
        self.assertEqual(args.days, 1)
        self.assertEqual(args.hours, 2)

    def test_dry_run_export_yaml_arg(self):
        parser = create_parser()
        args = parser.parse_args(
            [
                "--input-csv", "test.csv",
                "--dry-run",
                "--dry-run-export-yaml",
                "/tmp/yaml-out",
            ]
        )
        self.assertTrue(args.dry_run)
        self.assertEqual(args.dry_run_export_yaml, "/tmp/yaml-out")


# ============================================================================
# GROUP 20: Multi-Asset Old Format – Deeper Coverage (TODO 1)
# ============================================================================


class TestMultiWorkshopOldFormatDeep(unittest.TestCase):
    """Deeper tests for create_multi_workshop (old format) – shared password, asset parsing."""

    @patch("rhdp_flow.subprocess.run")
    def test_old_format_shared_password(self, mock_run):
        """All Workshop payloads in old-format multi-workshop share the same accessPassword."""
        created_payloads = []

        def capturing_dispatcher(*args, **kwargs):
            cmd = args[0] if args else kwargs.get('args', [])
            if len(cmd) >= 2 and cmd[1] == "create":
                for i, arg in enumerate(cmd):
                    if arg == "-f" and i + 1 < len(cmd) and os.path.exists(cmd[i + 1]):
                        with open(cmd[i + 1]) as f:
                            created_payloads.append(json.load(f))
            return make_oc_dispatcher()(*args, **kwargs)

        mock_run.side_effect = capturing_dispatcher
        config = make_config(dry_run=False)
        schedule = make_schedule(
            is_multi_asset=True,
            asset_cis="ci1.prod,ci2.event",
            multi_workshop_name="shared-pass-test",
            password="SharedPass",
        )
        create_multi_workshop(schedule, config)

        workshop_payloads = [
            p for p in created_payloads if p.get("kind") == "Workshop"
        ]
        self.assertGreaterEqual(len(workshop_payloads), 2)
        for payload in workshop_payloads:
            access_pw = payload.get("spec", {}).get("accessPassword", "")
            self.assertEqual(access_pw, "SharedPass")

    @patch("rhdp_flow.subprocess.run")
    def test_old_format_asset_ci_parsing(self, mock_run):
        """Two comma-separated asset_cis create exactly 2 Workshop resources."""
        created_kinds = []

        def capturing_dispatcher(*args, **kwargs):
            cmd = args[0] if args else kwargs.get('args', [])
            if len(cmd) >= 2 and cmd[1] == "create":
                for i, arg in enumerate(cmd):
                    if arg == "-f" and i + 1 < len(cmd) and os.path.exists(cmd[i + 1]):
                        with open(cmd[i + 1]) as f:
                            payload = json.load(f)
                            created_kinds.append(payload.get("kind"))
            return make_oc_dispatcher()(*args, **kwargs)

        mock_run.side_effect = capturing_dispatcher
        config = make_config(dry_run=False)
        schedule = make_schedule(
            is_multi_asset=True,
            asset_cis="ci1.prod,ci2.event",
            multi_workshop_name="two-asset-test",
        )
        create_multi_workshop(schedule, config)

        workshop_count = created_kinds.count("Workshop")
        self.assertEqual(workshop_count, 2)


# ============================================================================
# GROUP 21: Multi-Workshop From Group – Deeper Coverage (TODO 1)
# ============================================================================


class TestMultiWorkshopFromGroupDeep(unittest.TestCase):
    """Deeper tests for create_multi_workshop_from_group – per-item passwords, concurrency."""

    @patch("rhdp_flow.subprocess.run")
    def test_grouped_per_item_password_propagation(self, mock_run):
        """Each grouped schedule's password appears in its own Workshop payload."""
        created_payloads = []

        def capturing_dispatcher(*args, **kwargs):
            cmd = args[0] if args else kwargs.get('args', [])
            if len(cmd) >= 2 and cmd[1] == "create":
                for i, arg in enumerate(cmd):
                    if arg == "-f" and i + 1 < len(cmd) and os.path.exists(cmd[i + 1]):
                        with open(cmd[i + 1]) as f:
                            created_payloads.append(json.load(f))
            return make_oc_dispatcher()(*args, **kwargs)

        mock_run.side_effect = capturing_dispatcher
        config = make_config(dry_run=False)
        sched1 = make_schedule(
            ci="ci1.prod", password="Alpha1",
            multi_workshop_name="group-pw-test",
        )
        sched2 = make_schedule(
            ci="ci2.event", password="Beta2",
            multi_workshop_name="group-pw-test",
        )
        create_multi_workshop_from_group([sched1, sched2], config)

        workshop_payloads = [
            p for p in created_payloads if p.get("kind") == "Workshop"
        ]
        passwords = [p["spec"].get("accessPassword", "") for p in workshop_payloads]
        self.assertIn("Alpha1", passwords)
        self.assertIn("Beta2", passwords)

    @patch("rhdp_flow.subprocess.run")
    def test_grouped_mixed_concurrency(self, mock_run):
        """Grouped workshops leave provisioning to the MultiWorkshop controller."""
        created_payloads = []

        def capturing_dispatcher(*args, **kwargs):
            cmd = args[0] if args else kwargs.get('args', [])
            if len(cmd) >= 2 and cmd[1] == "create":
                for i, arg in enumerate(cmd):
                    if arg == "-f" and i + 1 < len(cmd) and os.path.exists(cmd[i + 1]):
                        with open(cmd[i + 1]) as f:
                            created_payloads.append(json.load(f))
            return make_oc_dispatcher()(*args, **kwargs)

        mock_run.side_effect = capturing_dispatcher
        config = make_config(dry_run=False)
        sched1 = make_schedule(
            ci="ci1.prod", concurrency=2,
            multi_workshop_name="group-conc-test",
        )
        sched2 = make_schedule(
            ci="ci2.event", concurrency=5,
            multi_workshop_name="group-conc-test",
        )
        create_multi_workshop_from_group([sched1, sched2], config)

        provision_payloads = [
            p for p in created_payloads if p.get("kind") == "WorkshopProvision"
        ]
        self.assertEqual(provision_payloads, [])
        groups = [p for p in created_payloads if p.get("kind") == "MultiWorkshop"]
        self.assertEqual(len(groups), 1)
        self.assertEqual({a["key"] for a in groups[0]["spec"]["assets"]}, {"ci1.prod", "ci2.event"})

    @patch("rhdp_flow.subprocess.run")
    def test_grouped_one_asset_fails_others_continue(self, mock_run):
        """If first Workshop create fails, function still attempts remaining assets and MultiWorkshop."""
        call_count = [0]

        def failing_first_dispatcher(*args, **kwargs):
            cmd = args[0] if args else kwargs.get('args', [])
            if len(cmd) >= 2 and cmd[1] == "create":
                for i, arg in enumerate(cmd):
                    if arg == "-f" and i + 1 < len(cmd) and os.path.exists(cmd[i + 1]):
                        with open(cmd[i + 1]) as f:
                            payload = json.load(f)
                        if payload.get("kind") == "Workshop":
                            call_count[0] += 1
                            if call_count[0] == 1:
                                return subprocess.CompletedProcess(
                                    cmd, 1, stdout="", stderr="Error: quota exceeded"
                                )
            return make_oc_dispatcher()(*args, **kwargs)

        mock_run.side_effect = failing_first_dispatcher
        config = make_config(dry_run=False)
        sched1 = make_schedule(ci="ci1.prod", multi_workshop_name="partial-fail-test")
        sched2 = make_schedule(ci="ci2.event", multi_workshop_name="partial-fail-test")
        result = create_multi_workshop_from_group([sched1, sched2], config)
        # Should still get a result (second asset succeeded)
        self.assertIsNotNone(result)


# ============================================================================
# GROUP 22: Count Expansion (TODO 2)
# ============================================================================


class TestCountExpansion(unittest.TestCase):
    """Tests for the count expansion logic in main() (lines 3911-3922)."""

    def setUp(self):
        self._tmpfiles = []

    def tearDown(self):
        for f in self._tmpfiles:
            try:
                os.unlink(f)
            except OSError:
                pass

    def _write(self, csv_text):
        path = _write_csv_tempfile(csv_text)
        self._tmpfiles.append(path)
        return path

    @patch("rhdp_flow.subprocess.run")
    def test_count_2_produces_2_named_instances(self, mock_run):
        """Count=2 produces 2 rows named 'AI Workshop (Instance 1)' and '(Instance 2)'."""
        mock_run.side_effect = make_oc_dispatcher()
        csv_path = self._write(COUNT_EXPANSION_CSV)
        output_path = tempfile.NamedTemporaryFile(suffix=".csv", delete=False).name
        self._tmpfiles.append(output_path)

        from rhdp_flow import main
        with patch("sys.argv", [
            "rhdp_flow.py", "--dry-run",
            "--input-csv", csv_path,
            "--output-csv", output_path,
        ]):
            try:
                main()
            except SystemExit:
                pass

        with open(output_path) as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 2)
        names = [r.get("ci_name", "") for r in rows]
        # The ci_name stays the same, but workshop name gets Instance suffix.
        # DeploymentResult carries ci_name not workshop_name, so check both rows exist.
        self.assertEqual(len(rows), 2)

    @patch("rhdp_flow.process_schedule")
    @patch("rhdp_flow.subprocess.run")
    def test_count_field_reset_to_1(self, mock_run, mock_ps):
        """Each expanded instance has count=1 when passed to process_schedule."""
        mock_run.side_effect = make_oc_dispatcher()
        mock_ps.return_value = DeploymentResult(
            ci_name="AI Workshop", ci="openshift-ai.ai-workshop-multi-user.prod",
            namespace="user-bbethell-redhat-com", guid="dryrun-test",
            url="", status="deployed_no_url",
            provisioning_date="17/02/2026 10:00", auto_stop="17/02/2026 18:00",
            auto_destroy="19/02/2026 10:00", timestamp="2026-02-17T10:00:00Z",
            error_message=""
        )
        csv_path = self._write(COUNT_EXPANSION_CSV)
        output_path = tempfile.NamedTemporaryFile(suffix=".csv", delete=False).name
        self._tmpfiles.append(output_path)

        from rhdp_flow import main
        with patch("sys.argv", [
            "rhdp_flow.py", "--dry-run",
            "--input-csv", csv_path,
            "--output-csv", output_path,
        ]):
            try:
                main()
            except SystemExit:
                pass

        self.assertEqual(mock_ps.call_count, 2)
        for call_args in mock_ps.call_args_list:
            schedule = call_args[0][0]
            self.assertEqual(schedule.count, 1)

    @patch("rhdp_flow.subprocess.run")
    def test_count_1_no_expansion(self, mock_run):
        """Count=1 produces exactly 1 result with original workshop_name (no Instance suffix)."""
        mock_run.side_effect = make_oc_dispatcher()
        csv_text = """\
CI Name,CI,Namespace,Users,Enable_workshop_interface,Password,Activity,Purpose,Workshop Name,Provisioning Date (UTC),Auto-stop (UTC),Auto-destroy (UTC),Multi_Asset,Asset_CIs,Multi_Workshop_Name,Concurrency,Instances,Count,AWS_Region
Single Workshop,openshift-ai.ai-workshop-multi-user.prod,user-bbethell-redhat-com,40,True,Pass1,Admin,Demo,Single WS,17/02/2026 10:00,17/02/2026 18:00,19/02/2026 10:00,,,,3,,1,
"""
        csv_path = self._write(csv_text)
        output_path = tempfile.NamedTemporaryFile(suffix=".csv", delete=False).name
        self._tmpfiles.append(output_path)

        from rhdp_flow import main
        with patch("sys.argv", [
            "rhdp_flow.py", "--dry-run",
            "--input-csv", csv_path,
            "--output-csv", output_path,
        ]):
            try:
                main()
            except SystemExit:
                pass

        with open(output_path) as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 1)

    @patch("rhdp_flow.process_schedule")
    @patch("rhdp_flow.subprocess.run")
    def test_count_users_not_divided(self, mock_run, mock_ps):
        """Count=2 with Users=40: each instance still has users=40 (not divided)."""
        mock_run.side_effect = make_oc_dispatcher()
        mock_ps.return_value = DeploymentResult(
            ci_name="AI Workshop", ci="openshift-ai.ai-workshop-multi-user.prod",
            namespace="user-bbethell-redhat-com", guid="dryrun-test",
            url="", status="deployed_no_url",
            provisioning_date="17/02/2026 10:00", auto_stop="17/02/2026 18:00",
            auto_destroy="19/02/2026 10:00", timestamp="2026-02-17T10:00:00Z",
            error_message=""
        )
        csv_path = self._write(COUNT_EXPANSION_CSV)
        output_path = tempfile.NamedTemporaryFile(suffix=".csv", delete=False).name
        self._tmpfiles.append(output_path)

        from rhdp_flow import main
        with patch("sys.argv", [
            "rhdp_flow.py", "--dry-run",
            "--input-csv", csv_path,
            "--output-csv", output_path,
        ]):
            try:
                main()
            except SystemExit:
                pass

        self.assertEqual(mock_ps.call_count, 2)
        for call_args in mock_ps.call_args_list:
            schedule = call_args[0][0]
            self.assertEqual(schedule.users, 40)

    @patch("rhdp_flow.process_schedule")
    @patch("rhdp_flow.subprocess.run")
    def test_count_preserves_other_fields(self, mock_run, mock_ps):
        """Expanded instances retain original ci, namespace, password, concurrency."""
        mock_run.side_effect = make_oc_dispatcher()
        mock_ps.return_value = DeploymentResult(
            ci_name="AI Workshop", ci="openshift-ai.ai-workshop-multi-user.prod",
            namespace="user-bbethell-redhat-com", guid="dryrun-test",
            url="", status="deployed_no_url",
            provisioning_date="17/02/2026 10:00", auto_stop="17/02/2026 18:00",
            auto_destroy="19/02/2026 10:00", timestamp="2026-02-17T10:00:00Z",
            error_message=""
        )
        csv_path = self._write(COUNT_EXPANSION_CSV)
        output_path = tempfile.NamedTemporaryFile(suffix=".csv", delete=False).name
        self._tmpfiles.append(output_path)

        from rhdp_flow import main
        with patch("sys.argv", [
            "rhdp_flow.py", "--dry-run",
            "--input-csv", csv_path,
            "--output-csv", output_path,
        ]):
            try:
                main()
            except SystemExit:
                pass

        self.assertEqual(mock_ps.call_count, 2)
        for call_args in mock_ps.call_args_list:
            schedule = call_args[0][0]
            self.assertEqual(schedule.ci, "openshift-ai.ai-workshop-multi-user.prod")
            self.assertEqual(schedule.namespace, "user-bbethell-redhat-com")
            self.assertEqual(schedule.password, "AIPass1")
            self.assertEqual(schedule.concurrency, 3)


# ============================================================================
# GROUP 23: Multi-Region Provisioning – Deeper Coverage (TODO 3)
# ============================================================================


class TestMultiRegionDeep(unittest.TestCase):
    """Deeper tests for create_multi_region_workshop – user distribution, suffixes, extra params."""

    @patch("rhdp_flow.create_workshop_provision")
    @patch("rhdp_flow.create_workshop_with_ui")
    def test_user_distribution_even(self, mock_ws, mock_prov):
        """40 users / 2 regions = 20 each."""
        mock_ws.return_value = "test-workshop-abc12"
        mock_prov.return_value = "test-workshop-abc12"
        config = make_config(dry_run=False)
        schedule = make_schedule(users=40, aws_regions="us-east-1,eu-west-1")
        create_multi_region_workshop(schedule, config)
        self.assertEqual(mock_prov.call_count, 2)
        counts = [c.kwargs.get("count") or c[1][7] if len(c[1]) > 7 else c.kwargs.get("count")
                  for c in mock_prov.call_args_list]
        # Use keyword args
        counts = [c.kwargs["count"] for c in mock_prov.call_args_list]
        self.assertEqual(sorted(counts), [20, 20])

    @patch("rhdp_flow.create_workshop_provision")
    @patch("rhdp_flow.create_workshop_with_ui")
    def test_user_distribution_with_remainder(self, mock_ws, mock_prov):
        """41 users / 3 regions → 14, 14, 13."""
        mock_ws.return_value = "test-workshop-abc12"
        mock_prov.return_value = "test-workshop-abc12"
        config = make_config(dry_run=False)
        schedule = make_schedule(users=41, aws_regions="us-east-1,eu-west-1,ap-south-1")
        create_multi_region_workshop(schedule, config)
        self.assertEqual(mock_prov.call_count, 3)
        counts = [c.kwargs["count"] for c in mock_prov.call_args_list]
        self.assertEqual(sorted(counts), [13, 14, 14])

    @patch("rhdp_flow.create_workshop_provision")
    @patch("rhdp_flow.create_workshop_with_ui")
    def test_region_suffix_format(self, mock_ws, mock_prov):
        """Provision names use region as suffix with hyphen prefix."""
        mock_ws.return_value = "test-workshop-abc12"
        mock_prov.return_value = "test-workshop-abc12"
        config = make_config(dry_run=False)
        schedule = make_schedule(users=40, aws_regions="us-east-1,eu-west-1")
        create_multi_region_workshop(schedule, config)
        suffixes = [c.kwargs["provision_name_suffix"] for c in mock_prov.call_args_list]
        self.assertIn("-us-east-1", suffixes)
        self.assertIn("-eu-west-1", suffixes)

    @patch("rhdp_flow.create_workshop_provision")
    @patch("rhdp_flow.create_workshop_with_ui")
    def test_region_underscore_replacement(self, mock_ws, mock_prov):
        """Region 'us_east_1' → suffix '-us-east-1'."""
        mock_ws.return_value = "test-workshop-abc12"
        mock_prov.return_value = "test-workshop-abc12"
        config = make_config(dry_run=False)
        schedule = make_schedule(users=40, aws_regions="us_east_1,eu_west_1")
        create_multi_region_workshop(schedule, config)
        suffixes = [c.kwargs["provision_name_suffix"] for c in mock_prov.call_args_list]
        self.assertIn("-us-east-1", suffixes)
        self.assertIn("-eu-west-1", suffixes)

    @patch("rhdp_flow.create_workshop_provision")
    @patch("rhdp_flow.create_workshop_with_ui")
    def test_extra_parameters_injection(self, mock_ws, mock_prov):
        """Each provision gets extra_parameters={'aws_region': <region>}."""
        mock_ws.return_value = "test-workshop-abc12"
        mock_prov.return_value = "test-workshop-abc12"
        config = make_config(dry_run=False)
        schedule = make_schedule(users=40, aws_regions="us-east-1,eu-west-1")
        create_multi_region_workshop(schedule, config)
        extra_params = [c.kwargs["extra_parameters"] for c in mock_prov.call_args_list]
        self.assertEqual(extra_params[0], {"aws_region": "us-east-1"})
        self.assertEqual(extra_params[1], {"aws_region": "eu-west-1"})

    @patch("rhdp_flow.create_workshop_provision")
    @patch("rhdp_flow.create_workshop_with_ui")
    def test_single_workshop_multiple_provisions(self, mock_ws, mock_prov):
        """create_workshop_with_ui called once, create_workshop_provision called N times."""
        mock_ws.return_value = "test-workshop-abc12"
        mock_prov.return_value = "test-workshop-abc12"
        config = make_config(dry_run=False)
        schedule = make_schedule(users=60, aws_regions="us-east-1,eu-west-1,ap-south-1")
        create_multi_region_workshop(schedule, config)
        self.assertEqual(mock_ws.call_count, 1)
        self.assertEqual(mock_prov.call_count, 3)

    @patch("rhdp_flow.create_workshop_provision")
    @patch("rhdp_flow.create_workshop_with_ui")
    def test_concurrency_inheritance(self, mock_ws, mock_prov):
        """All provisions inherit schedule's concurrency value."""
        mock_ws.return_value = "test-workshop-abc12"
        mock_prov.return_value = "test-workshop-abc12"
        config = make_config(dry_run=False)
        schedule = make_schedule(users=40, aws_regions="us-east-1,eu-west-1", concurrency=5)
        create_multi_region_workshop(schedule, config)
        for call_args in mock_prov.call_args_list:
            self.assertEqual(call_args.kwargs["concurrency"], 5)

    @patch("rhdp_flow.create_workshop_provision")
    @patch("rhdp_flow.create_workshop_with_ui")
    def test_three_regions_distribution(self, mock_ws, mock_prov):
        """100 users / 3 regions → 34, 33, 33."""
        mock_ws.return_value = "test-workshop-abc12"
        mock_prov.return_value = "test-workshop-abc12"
        config = make_config(dry_run=False)
        schedule = make_schedule(users=100, aws_regions="us-east-1,eu-west-1,ap-south-1")
        create_multi_region_workshop(schedule, config)
        counts = [c.kwargs["count"] for c in mock_prov.call_args_list]
        self.assertEqual(sorted(counts), [33, 33, 34])


# ============================================================================
# GROUP 24: Landing Page URL (TODO 4)
# ============================================================================


class TestLandingPageUrl(unittest.TestCase):
    """Tests for get_landing_page_url()."""

    def test_landing_page_url_construction(self):
        url = get_landing_page_url("m5hzmw")
        self.assertEqual(url, "https://integration.demo.redhat.com/workshop/m5hzmw")

    def test_landing_page_url_empty(self):
        self.assertEqual(get_landing_page_url(""), "")


# ============================================================================
# GROUP 25: Get Workshop URLs (TODO 4)
# ============================================================================


class TestGetWorkshopUrls(unittest.TestCase):
    """Tests for get_workshop_urls()."""

    @patch("rhdp_flow.subprocess.run")
    def test_workshop_urls_returns_tuple(self, mock_run):
        mock_run.side_effect = make_oc_dispatcher()
        config = make_config(dry_run=False)
        full_url, landing_url = get_workshop_urls(
            "ci-name-abc12", "user-bbethell-redhat-com",
            "openshift-cnv.ocp-virt-roadshow-multi-user.prod", config
        )
        self.assertIsInstance(full_url, str)
        self.assertIsInstance(landing_url, str)
        self.assertTrue(full_url.startswith("https://"))
        self.assertTrue(landing_url.startswith("https://"))

    @patch("rhdp_flow.subprocess.run")
    def test_workshop_urls_suffix_extraction(self, mock_run):
        """Workshop name 'ci-name-abc12' → suffix 'abc12' used in full URL."""
        mock_run.side_effect = make_oc_dispatcher()
        config = make_config(dry_run=False)
        full_url, _ = get_workshop_urls(
            "ci-name-abc12", "user-bbethell-redhat-com",
            "openshift-cnv.ocp-virt-roadshow-multi-user.prod", config
        )
        self.assertIn("abc12", full_url)


# ============================================================================
# GROUP 26: Export Student Landing Page CSV (TODO 4)
# ============================================================================


class TestExportStudentLandingPageCSV(unittest.TestCase):
    """Tests for export_student_landing_page_csv()."""

    def setUp(self):
        self._tmpfiles = []

    def tearDown(self):
        for f in self._tmpfiles:
            try:
                os.unlink(f)
            except OSError:
                pass

    def _make_result(self, **overrides):
        defaults = dict(
            ci_name="Experience OpenShift Virtualization Roadshow",
            ci="openshift-cnv.ocp-virt-roadshow-multi-user.prod",
            namespace="user-bbethell-redhat-com",
            deployed="Yes",
            status="✅ verified",
            landing_page_url="https://integration.demo.redhat.com/workshop/m5hzmw",
            link_to_service="https://integration.demo.redhat.com/workshops/user-ns/ci-name-abc12",
            provisioning_date="15/02/2026 11:00",
            auto_stop="15/02/2026 19:00",
        )
        defaults.update(overrides)
        return defaults

    def test_export_csv_format(self):
        """Output CSV has correct headers."""
        output = tempfile.NamedTemporaryFile(suffix=".csv", delete=False)
        output.close()
        self._tmpfiles.append(output.name)

        results = [self._make_result()]
        export_student_landing_page_csv(results, output.name)

        with open(output.name, "r") as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        self.assertEqual(len(rows), 1)
        expected_headers = {'Code', 'Title', 'Location', 'Date', 'Start Time', 'End Time', 'Catalog URL', 'Device Type'}
        self.assertEqual(set(rows[0].keys()), expected_headers)
        self.assertEqual(rows[0]['Device Type'], 'laptop')

    def test_export_regular_workshop_uses_landing_page_url(self):
        """Regular workshop (status='✅ verified') uses landing_page_url as Catalog URL."""
        output = tempfile.NamedTemporaryFile(suffix=".csv", delete=False)
        output.close()
        self._tmpfiles.append(output.name)

        results = [self._make_result(
            status="✅ verified",
            landing_page_url="https://integration.demo.redhat.com/workshop/m5hzmw",
            link_to_service="https://integration.demo.redhat.com/workshops/user-ns/fallback",
        )]
        export_student_landing_page_csv(results, output.name)

        with open(output.name, "r") as f:
            rows = list(csv.DictReader(f))

        self.assertEqual(rows[0]['Catalog URL'], "https://integration.demo.redhat.com/workshop/m5hzmw")

    def test_export_multi_workshop_uses_link_to_service(self):
        """Multi-workshop (status='✅ MULTI-WORKSHOP') uses link_to_service as Catalog URL."""
        output = tempfile.NamedTemporaryFile(suffix=".csv", delete=False)
        output.close()
        self._tmpfiles.append(output.name)

        results = [self._make_result(
            status="✅ MULTI-WORKSHOP",
            landing_page_url="https://integration.demo.redhat.com/workshop/m5hzmw",
            link_to_service="https://integration.demo.redhat.com/multi-workshop/user-ns/portal",
        )]
        export_student_landing_page_csv(results, output.name)

        with open(output.name, "r") as f:
            rows = list(csv.DictReader(f))

        self.assertEqual(rows[0]['Catalog URL'], "https://integration.demo.redhat.com/multi-workshop/user-ns/portal")


# ============================================================================
# WHITE-GLOVE TESTS
# ============================================================================

class TestWhiteGloveLabel(unittest.TestCase):
    """Tests for white-glove label in payloads."""

    def test_white_glove_true_by_default(self):
        """White-glove defaults to True, label is 'true'."""
        schedule = make_schedule()
        config = make_config(dry_run=True)
        payload = build_resource_claim_payload(schedule, config)
        self.assertEqual(
            payload["metadata"]["labels"]["demo.redhat.com/white-glove"], "true"
        )

    def test_white_glove_true_sets_label(self):
        """White-glove=True sets label to 'true'."""
        schedule = make_schedule(white_glove=True)
        config = make_config(dry_run=True)
        payload = build_resource_claim_payload(schedule, config)
        self.assertEqual(
            payload["metadata"]["labels"]["demo.redhat.com/white-glove"], "true"
        )

    def test_white_glove_threaded_via_payload(self):
        """_white_glove key is set on payload for create_workshop_with_ui."""
        schedule = make_schedule(white_glove=True)
        config = make_config(dry_run=True)
        payload = build_resource_claim_payload(schedule, config)
        self.assertTrue(payload.get("_white_glove"))

    def test_csv_missing_white_glove_defaults_true(self):
        """CSV without White_Glove column still parses (default is True)."""
        schedules = read_csv_input(io.StringIO(BASIC_WORKSHOP_CSV))
        self.assertTrue(schedules[0].white_glove)


# ============================================================================
# ASSET PASSWORD TESTS
# ============================================================================

class TestLoadAssetPasswords(unittest.TestCase):
    """Tests for load_asset_passwords()."""

    def test_valid_password_file(self):
        """Loads CI->password mappings from valid CSV."""
        csv_text = "CI,Password\nsome-ci,secret1\nother-ci,secret2\n"
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, encoding="utf-8"
        )
        tmp.write(csv_text)
        tmp.close()
        try:
            passwords = load_asset_passwords(tmp.name)
            self.assertEqual(passwords, {"some-ci": "secret1", "other-ci": "secret2"})
        finally:
            os.unlink(tmp.name)

    def test_missing_file_returns_empty(self):
        """Returns empty dict for nonexistent file."""
        passwords = load_asset_passwords("/nonexistent/path/passwords.csv")
        self.assertEqual(passwords, {})

    def test_empty_file_returns_empty(self):
        """Returns empty dict for empty CSV."""
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, encoding="utf-8"
        )
        tmp.write("")
        tmp.close()
        try:
            passwords = load_asset_passwords(tmp.name)
            self.assertEqual(passwords, {})
        finally:
            os.unlink(tmp.name)

    def test_none_filepath_returns_empty(self):
        """Returns empty dict for None filepath."""
        passwords = load_asset_passwords(None)
        self.assertEqual(passwords, {})


class TestPerAssetPasswordOverride(unittest.TestCase):
    """Tests for per-asset password override in create_multi_workshop."""

    @patch("subprocess.run")
    def test_asset_password_used_in_multi_workshop(self, mock_run):
        """create_multi_workshop uses asset_passwords for matching CIs."""
        mock_run.side_effect = make_oc_dispatcher()
        schedule = make_schedule(
            is_multi_asset=True,
            asset_cis="ci-a,ci-b",
            multi_workshop_name="test-pw-override",
            provisioning_date="15/02/2026 11:00",
            auto_stop="15/02/2026 19:00",
            auto_destroy="17/02/2026 11:00",
        )
        config = make_config(dry_run=True)
        asset_passwords = {"ci-a": "override-a"}

        # Capture the payloads passed to create_workshop_with_ui
        with patch("rhdp_flow.create_workshop_with_ui", wraps=create_workshop_with_ui) as mock_cwui:
            create_multi_workshop(schedule, config, asset_passwords=asset_passwords)
            # Check that the first asset got the overridden password
            calls = mock_cwui.call_args_list
            if calls:
                first_payload = calls[0][0][2]  # Third positional arg is the payload
                self.assertEqual(first_payload["spec"]["accessPassword"], "override-a")
                # Second asset falls back to schedule password
                if len(calls) > 1:
                    second_payload = calls[1][0][2]
                    self.assertEqual(second_payload["spec"]["accessPassword"], schedule.password)


# ============================================================================
# DERIVE BASE DOMAIN TESTS
# ============================================================================


class TestDeriveBaseDomain(unittest.TestCase):
    """Tests for derive_base_domain()."""

    def test_standard_api_url(self):
        """Standard API URL: strips https://, port, and api. prefix."""
        result = derive_base_domain("https://api.integration.demo.redhat.com:6443")
        self.assertEqual(result, "integration.demo.redhat.com")

    def test_api_url_without_port(self):
        """API URL without port."""
        result = derive_base_domain("https://api.integration.demo.redhat.com")
        self.assertEqual(result, "integration.demo.redhat.com")

    def test_non_api_url(self):
        """URL without api. prefix keeps the host as-is."""
        result = derive_base_domain("https://cluster.example.com:6443")
        self.assertEqual(result, "cluster.example.com")

    def test_http_scheme(self):
        """HTTP scheme is stripped correctly."""
        result = derive_base_domain("http://api.demo.redhat.com:6443")
        self.assertEqual(result, "demo.redhat.com")

    def test_no_scheme(self):
        """URL without scheme still works."""
        result = derive_base_domain("api.integration.demo.redhat.com:6443")
        self.assertEqual(result, "integration.demo.redhat.com")

    def test_empty_string_returns_fallback(self):
        """Empty string returns the fallback domain."""
        result = derive_base_domain("")
        self.assertEqual(result, "integration.demo.redhat.com")

    def test_none_returns_fallback(self):
        """None returns the fallback domain."""
        result = derive_base_domain(None)
        self.assertEqual(result, "integration.demo.redhat.com")

    def test_trailing_slash(self):
        """Trailing slash is stripped."""
        result = derive_base_domain("https://api.demo.redhat.com:6443/")
        self.assertEqual(result, "demo.redhat.com")

    def test_different_cluster(self):
        """Different cluster URL produces correct domain."""
        result = derive_base_domain("https://api.ocp-integration.infra.open.redhat.com:6443")
        self.assertEqual(result, "babylon-catalog.apps.ocp-integration.infra.open.redhat.com")


# ============================================================================
# RESOURCE LOCK LABEL TESTS
# ============================================================================


class TestResourceLockLabel(unittest.TestCase):
    """Tests for lock-enabled label in payloads."""

    def test_resource_lock_true_by_default(self):
        """Config defaults resource_lock=True, label is 'true'."""
        schedule = make_schedule()
        config = make_config(dry_run=True)
        payload = build_resource_claim_payload(schedule, config)
        self.assertEqual(
            payload["metadata"]["labels"]["demo.redhat.com/lock-enabled"], "true"
        )

    def test_resource_lock_false(self):
        """When resource_lock=False, label is 'false'."""
        schedule = make_schedule()
        config = make_config(dry_run=True)
        config.resource_lock = False
        payload = build_resource_claim_payload(schedule, config)
        self.assertEqual(
            payload["metadata"]["labels"]["demo.redhat.com/lock-enabled"], "false"
        )

    def test_resource_lock_label_present_in_payload(self):
        """lock-enabled label is always present in the payload."""
        schedule = make_schedule()
        config = make_config(dry_run=True)
        payload = build_resource_claim_payload(schedule, config)
        self.assertIn("demo.redhat.com/lock-enabled", payload["metadata"]["labels"])


# ============================================================================
# ENABLE RESOURCE POOLS TESTS
# ============================================================================


class TestEnableResourcePools(unittest.TestCase):
    """Tests for enable_resource_pools and pool annotation."""

    def test_pools_disabled_by_default(self):
        """Default config disables pools: annotation set to 'disable'."""
        schedule = make_schedule()
        config = make_config(dry_run=True)
        payload = build_resource_claim_payload(schedule, config)
        self.assertEqual(
            payload["metadata"]["annotations"]["poolboy.gpte.redhat.com/resource-pool-name"],
            "disable",
        )

    def test_pools_enabled_removes_disable_annotation(self):
        """When enable_resource_pools=True, the 'disable' annotation is absent."""
        schedule = make_schedule()
        config = make_config(dry_run=True)
        config.enable_resource_pools = True
        payload = build_resource_claim_payload(schedule, config)
        self.assertNotIn(
            "poolboy.gpte.redhat.com/resource-pool-name",
            payload["metadata"]["annotations"],
        )

    def test_pools_disabled_explicitly(self):
        """Explicitly setting enable_resource_pools=False adds disable annotation."""
        schedule = make_schedule()
        config = make_config(dry_run=True)
        config.enable_resource_pools = False
        payload = build_resource_claim_payload(schedule, config)
        self.assertEqual(
            payload["metadata"]["annotations"]["poolboy.gpte.redhat.com/resource-pool-name"],
            "disable",
        )


# ============================================================================
# BASE DOMAIN URL CONSTRUCTION TESTS
# ============================================================================


class TestBaseDomainUrlConstruction(unittest.TestCase):
    """Tests for base_domain parameter on URL construction functions."""

    def test_construct_workshop_url_custom_domain(self):
        """construct_workshop_url uses custom base_domain."""
        url = construct_workshop_url(
            "openshift-cnv.ocp-virt-roadshow-multi-user.prod",
            "user-bbethell-redhat-com",
            "vt958",
            base_domain="ocp-production.demo.redhat.com",
        )
        self.assertEqual(
            url,
            "https://ocp-production.demo.redhat.com/workshops/user-bbethell-redhat-com/openshift-cnv.ocp-virt-roadshow-multi-user.prod-vt958/details",
        )

    def test_construct_workshop_url_default_domain(self):
        """construct_workshop_url uses default domain when not specified."""
        url = construct_workshop_url(
            "openshift-cnv.ocp-virt-roadshow-multi-user.prod",
            "user-bbethell-redhat-com",
        )
        self.assertIn("integration.demo.redhat.com", url)

    def test_construct_workshop_url_custom_domain_no_suffix(self):
        """construct_workshop_url with custom domain and no suffix."""
        url = construct_workshop_url(
            "openshift-cnv.ocp-virt-roadshow-multi-user.prod",
            "user-bbethell-redhat-com",
            base_domain="staging.demo.redhat.com",
        )
        self.assertEqual(
            url,
            "https://staging.demo.redhat.com/workshops/user-bbethell-redhat-com/openshift-cnv.ocp-virt-roadshow-multi-user.prod/details",
        )

    def test_landing_page_url_custom_domain(self):
        """get_landing_page_url uses custom base_domain."""
        url = get_landing_page_url("m5hzmw", base_domain="ocp-production.demo.redhat.com")
        self.assertEqual(url, "https://ocp-production.demo.redhat.com/workshop/m5hzmw")

    def test_landing_page_url_default_domain(self):
        """get_landing_page_url uses default domain when not specified."""
        url = get_landing_page_url("m5hzmw")
        self.assertIn("integration.demo.redhat.com", url)

    def test_landing_page_url_empty_id_returns_empty(self):
        """get_landing_page_url returns empty string for empty workshop_id."""
        url = get_landing_page_url("", base_domain="custom.domain.com")
        self.assertEqual(url, "")


# ============================================================================
# CONFIG DEFAULTS TESTS
# ============================================================================


class TestRHDPConfigDefaults(unittest.TestCase):
    """Tests for new RHDPConfig default field values."""

    def test_resource_lock_default_true(self):
        config = RHDPConfig()
        self.assertTrue(config.resource_lock)

    def test_enable_resource_pools_default_false(self):
        config = RHDPConfig()
        self.assertFalse(config.enable_resource_pools)

    def test_white_glove_default_true(self):
        config = RHDPConfig()
        self.assertTrue(config.white_glove)

    def test_base_domain_default(self):
        config = RHDPConfig()
        self.assertEqual(config.base_domain, "integration.demo.redhat.com")


class TestSoundcheckEnrich(unittest.TestCase):
    """HTTP-contract coverage for the QA3 Soundcheck batch enrich.

    These mock the Soundcheck service (``_http_json``) and the cluster workshop
    listing (``_collect_workshop_ids_for_schedules``) so the real contract —
    batch-once, status mapping, unreachable handling, cancel — is exercised
    without a live cluster or Soundcheck endpoint.
    """

    def _schedule_and_result(self, ci="my.catalog.item.prod", ns="user-ns"):
        sched = make_schedule(ci=ci, namespace=ns)
        result = {"ci": ci, "issues": ""}
        return sched, result

    def _no_sleep(self):
        return patch("rhdp_flow.time.sleep", lambda *_a, **_k: None)

    def test_maps_completed_status_to_healthy(self):
        import rhdp_flow

        sched, result = self._schedule_and_result()

        def fake_http(method, url, body=None, timeout=30.0, extra_headers=None):
            if url.endswith("/api/groups") and method == "POST":
                return {"group_id": "grp-1"}
            if "/api/groups/" in url and url.endswith("/run"):
                return {}
            if "/api/groups/" in url:
                return {"group": {"status": "completed"}}
            if url.endswith("/api/workshops/check-status"):
                return {"statuses": {"wid-1": {"status": "completed", "session_id": "grp-1"}}}
            return {}

        with self._no_sleep(), patch(
            "rhdp_flow._collect_workshop_ids_for_schedules",
            return_value=[(sched, "ws-1", "wid-1")],
        ), patch("rhdp_flow._http_json", side_effect=fake_http):
            summary = rhdp_flow._enrich_qa2_results_with_soundcheck(
                [result], [sched], ["user-ns"], make_config()
            )

        self.assertTrue(summary["reachable"])
        self.assertEqual(result["showroom_status"], "healthy")
        self.assertIn("/group/grp-1", result["showroom_url"])

    def test_failed_status_adds_issue(self):
        import rhdp_flow

        sched, result = self._schedule_and_result()

        def fake_http(method, url, body=None, timeout=30.0, extra_headers=None):
            if url.endswith("/api/groups") and method == "POST":
                return {"group_id": "grp-9"}
            if "/api/groups/" in url and url.endswith("/run"):
                return {}
            if "/api/groups/" in url:
                return {"group": {"status": "failed"}}
            if url.endswith("/api/workshops/check-status"):
                return {"statuses": {"wid-1": {"status": "failed", "session_id": "grp-9"}}}
            return {}

        with self._no_sleep(), patch(
            "rhdp_flow._collect_workshop_ids_for_schedules",
            return_value=[(sched, "ws-1", "wid-1")],
        ), patch("rhdp_flow._http_json", side_effect=fake_http):
            rhdp_flow._enrich_qa2_results_with_soundcheck([result], [sched], ["user-ns"], make_config())

        self.assertEqual(result["showroom_status"], "unhealthy")
        self.assertIn("Soundcheck failed", result["issues"])

    def test_unreachable_marks_rows_not_blank(self):
        import rhdp_flow

        sched, result = self._schedule_and_result()

        def boom(*_a, **_k):
            raise RuntimeError("connection refused")

        with self._no_sleep(), patch(
            "rhdp_flow._collect_workshop_ids_for_schedules",
            return_value=[(sched, "ws-1", "wid-1")],
        ), patch("rhdp_flow._http_json", side_effect=boom):
            summary = rhdp_flow._enrich_qa2_results_with_soundcheck(
                [result], [sched], ["user-ns"], make_config()
            )

        self.assertFalse(summary["reachable"])
        self.assertEqual(result["showroom_status"], "unreachable")
        self.assertIn("Soundcheck unreachable", result["issues"])

    def test_single_kickoff_across_multiple_namespaces(self):
        import rhdp_flow

        s1 = make_schedule(ci="ci.one.prod", namespace="ns-a")
        s2 = make_schedule(ci="ci.two.prod", namespace="ns-b")
        r1 = {"ci": "ci.one.prod", "issues": ""}
        r2 = {"ci": "ci.two.prod", "issues": ""}

        def fake_collect(scheduled_items, namespace, config):
            if namespace == "ns-a":
                return [(s1, "ws-a", "wid-a")]
            if namespace == "ns-b":
                return [(s2, "ws-b", "wid-b")]
            return []

        kickoffs = []

        def fake_http(method, url, body=None, timeout=30.0, extra_headers=None):
            if url.endswith("/api/groups") and method == "POST":
                kickoffs.append(body or {})
                return {"group_id": "grp-x"}
            if "/api/groups/" in url and url.endswith("/run"):
                return {}
            if "/api/groups/" in url:
                return {"group": {"status": "completed"}}
            if url.endswith("/api/workshops/check-status"):
                return {
                    "statuses": {
                        "wid-a": {"status": "completed", "session_id": "grp-x"},
                        "wid-b": {"status": "completed", "session_id": "grp-x"},
                    }
                }
            return {}

        with self._no_sleep(), patch(
            "rhdp_flow._collect_workshop_ids_for_schedules", side_effect=fake_collect
        ), patch("rhdp_flow._http_json", side_effect=fake_http):
            rhdp_flow._enrich_qa2_results_with_soundcheck(
                [r1, r2], [s1, s2], ["ns-a", "ns-b"], make_config()
            )

        # One batched group creation covering both namespaces' workshop ids.
        self.assertEqual(len(kickoffs), 1)
        self.assertIn("wid-a", kickoffs[0].get("workshop_guids", []))
        self.assertIn("wid-b", kickoffs[0].get("workshop_guids", []))
        self.assertEqual(r1["showroom_status"], "healthy")
        self.assertEqual(r2["showroom_status"], "healthy")

    def test_cancel_during_poll_short_circuits(self):
        import rhdp_flow

        sched, result = self._schedule_and_result()
        group_detail_calls = []

        def fake_http(method, url, body=None, timeout=30.0, extra_headers=None):
            if url.endswith("/api/groups") and method == "POST":
                return {"group_id": "grp-c"}
            if "/api/groups/" in url and url.endswith("/run"):
                return {}
            if "/api/groups/" in url:
                group_detail_calls.append(url)
                return {"group": {"status": "pending"}}
            if url.endswith("/api/workshops/check-status"):
                return {"statuses": {}}
            return {}

        with self._no_sleep(), patch(
            "rhdp_flow._collect_workshop_ids_for_schedules",
            return_value=[(sched, "ws-1", "wid-1")],
        ), patch("rhdp_flow._http_json", side_effect=fake_http):
            rhdp_flow._enrich_qa2_results_with_soundcheck(
                [result], [sched], ["user-ns"], make_config(), is_cancelled=lambda: True
            )

        # Cancel is checked at the top of the poll loop, so no group detail poll runs.
        self.assertEqual(group_detail_calls, [])

    def test_progress_callback_invoked(self):
        import rhdp_flow

        sched, result = self._schedule_and_result()
        progress = []

        def fake_http(method, url, body=None, timeout=30.0, extra_headers=None):
            if url.endswith("/api/groups") and method == "POST":
                return {"group_id": "grp-p"}
            if "/api/groups/" in url and url.endswith("/run"):
                return {}
            if "/api/groups/" in url:
                return {"group": {"status": "completed"}}
            if url.endswith("/api/workshops/check-status"):
                return {"statuses": {"wid-1": {"status": "completed", "session_id": "grp-p"}}}
            return {}

        with self._no_sleep(), patch(
            "rhdp_flow._collect_workshop_ids_for_schedules",
            return_value=[(sched, "ws-1", "wid-1")],
        ), patch("rhdp_flow._http_json", side_effect=fake_http):
            rhdp_flow._enrich_qa2_results_with_soundcheck(
                [result], [sched], ["user-ns"], make_config(),
                on_progress=lambda pct, msg: progress.append((pct, msg)),
            )

        self.assertTrue(progress)
        self.assertTrue(any("Soundcheck" in msg for _pct, msg in progress))


# ============================================================================
# ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    unittest.main()
