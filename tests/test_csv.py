"""Tests for read_csv_input and write_deployment_results."""

import csv
import io
import os
import tempfile

import pytest

from rhdp_flow import (
    DeploymentResult,
    load_asset_num_users,
    load_asset_passwords,
    read_csv_input,
    write_deployment_results,
)
from tests.conftest import (
    BASIC_WORKSHOP_CSV,
    INSTANCES_AND_CONCURRENCY_CSV,
    MISSING_HEADERS_CSV,
    MULTI_ASSET_GROUPED_CSV,
    MULTI_ASSET_OLD_CSV,
    OLD_DATE_HEADERS_CSV,
    write_csv_tempfile,
)


class TestCSVParsing:
    def setup_method(self):
        self._tmpfiles = []

    def teardown_method(self):
        for f in self._tmpfiles:
            try:
                os.unlink(f)
            except OSError:
                pass

    def _write(self, csv_text):
        path = write_csv_tempfile(csv_text)
        self._tmpfiles.append(path)
        return path

    def test_basic_single_row(self):
        path = self._write(BASIC_WORKSHOP_CSV)
        schedules = read_csv_input(path)
        assert len(schedules) == 1
        s = schedules[0]
        assert s.ci_name == "Experience OpenShift Virtualization Roadshow"
        assert s.ci == "openshift-cnv.ocp-virt-roadshow-multi-user.prod"
        assert s.white_glove is True  # default when White_Glove column omitted
        assert s.namespace == "user-bbethell-redhat-com"
        assert s.users == 20
        assert s.enable_workshop_interface is True
        assert s.password == "Workshop1"

    def test_white_glove_column_false(self):
        csv_text = """\
CI Name,CI,Namespace,Users,Enable_workshop_interface,Password,Activity,Purpose,Workshop Name,Provisioning Date (UTC),Auto-stop (UTC),Auto-destroy (UTC),White_Glove
WG Off,openshift-cnv.ocp-virt-roadshow-multi-user.prod,user-bbethell-redhat-com,20,True,Pass1,Admin,QA,WG Test,15/02/2026 11:00,15/02/2026 19:00,17/02/2026 11:00,False
"""
        path = self._write(csv_text)
        schedules = read_csv_input(path)
        assert len(schedules) == 1
        assert schedules[0].white_glove is False

    def test_multi_asset_old_format(self):
        path = self._write(MULTI_ASSET_OLD_CSV)
        schedules = read_csv_input(path)
        assert len(schedules) == 1
        assert schedules[0].is_multi_asset is True
        assert "openshift-cnv" in schedules[0].asset_cis

    def test_grouped_multi_asset(self):
        path = self._write(MULTI_ASSET_GROUPED_CSV)
        schedules = read_csv_input(path)
        assert len(schedules) == 2
        assert schedules[0].multi_workshop_name == "summit-demo-2026"
        assert schedules[0].is_multi_asset is False

    def test_instances_and_concurrency(self):
        path = self._write(INSTANCES_AND_CONCURRENCY_CSV)
        schedules = read_csv_input(path)
        assert schedules[0].instances == 30
        assert schedules[0].concurrency == 3

    def test_instances_column_parsed(self):
        """Instances column sets schedule.instances."""
        csv_text = (
            "CI Name,CI,Namespace,Users,Enable_workshop_interface,Password,Activity,Purpose,"
            "Workshop Name,Provisioning Date (UTC),Auto-stop (UTC),Auto-destroy (UTC),"
            "Multi_Asset,Asset_CIs,Multi_Workshop_Name,Concurrency,Instances,Salesforce IDs\n"
            "Test,test.ci.prod,user-test-ns,10,TRUE,pw,Admin,QA,,"
            "15/02/2026 11:00,15/02/2026 19:00,17/02/2026 11:00,,,,,2,\n"
        )
        path = self._write(csv_text)
        schedules = read_csv_input(path)
        assert schedules[0].instances == 2

    def test_no_instances_column_in_header(self):
        """No Instances column → schedule.instances is None."""
        csv_text = (
            "CI Name,CI,Namespace,Users,Enable_workshop_interface,Password,Activity,Purpose,"
            "Workshop Name,Provisioning Date (UTC),Auto-stop (UTC),Auto-destroy (UTC)\n"
            "Test,test.ci.prod,user-test-ns,10,TRUE,pw,Admin,QA,,"
            "15/02/2026 11:00,15/02/2026 19:00,17/02/2026 11:00\n"
        )
        path = self._write(csv_text)
        schedules = read_csv_input(path)
        assert schedules[0].instances is None

    def test_instances_column_blank_cell(self):
        """Instances column present but empty cell → schedule.instances is None."""
        csv_text = (
            "CI Name,CI,Namespace,Users,Enable_workshop_interface,Password,Activity,Purpose,"
            "Workshop Name,Provisioning Date (UTC),Auto-stop (UTC),Auto-destroy (UTC),"
            "Multi_Asset,Asset_CIs,Multi_Workshop_Name,Concurrency,Instances,Salesforce IDs\n"
            "Test,test.ci.prod,user-test-ns,10,TRUE,pw,Admin,QA,,"
            "15/02/2026 11:00,15/02/2026 19:00,17/02/2026 11:00,,,,,,\n"
        )
        path = self._write(csv_text)
        schedules = read_csv_input(path)
        assert schedules[0].instances is None

    def test_missing_required_headers_raises(self):
        path = self._write(MISSING_HEADERS_CSV)
        with pytest.raises(ValueError):
            read_csv_input(path)

    def test_empty_csv_raises(self):
        path = self._write(
            "CI Name,CI,Namespace,Users,Enable_workshop_interface,Password,"
            "Activity,Purpose,Provisioning Date (UTC),Auto-stop (UTC),Auto-destroy (UTC)\n"
        )
        with pytest.raises(ValueError):
            read_csv_input(path)

    def test_old_date_headers(self):
        path = self._write(OLD_DATE_HEADERS_CSV)
        schedules = read_csv_input(path)
        assert len(schedules) == 1
        assert schedules[0].provisioning_date == "15/02/2026 11:00"

    def test_write_deployment_results_roundtrip(self):
        results = [
            DeploymentResult(
                ci_name="Test", ci="test-ci", namespace="test-ns",
                guid="test-guid", url="https://example.com",
                status="verified", provisioning_date="15/02/2026 11:00",
                auto_stop="15/02/2026 19:00", auto_destroy="17/02/2026 11:00",
                timestamp="2026-02-15T11:00:00Z", error_message="",
                password="workshop-secret",
            )
        ]
        output = tempfile.NamedTemporaryFile(suffix=".csv", delete=False)
        output.close()
        self._tmpfiles.append(output.name)
        write_deployment_results(results, output.name)
        with open(output.name) as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 1
        assert rows[0]["ci_name"] == "Test"
        assert rows[0]["status"] == "verified"
        assert rows[0]["password"] == "workshop-secret"

    def test_skip_incomplete_rows(self):
        csv_text = """\
CI Name,CI,Namespace,Users,Enable_workshop_interface,Password,Activity,Purpose,Workshop Name,Provisioning Date (UTC),Auto-stop (UTC),Auto-destroy (UTC)
Valid Row,valid-ci,valid-ns,20,True,pass,Admin,QA,My Workshop,15/02/2026 11:00,15/02/2026 19:00,17/02/2026 11:00
,,,,,,,,,,,
"""
        path = self._write(csv_text)
        schedules = read_csv_input(path)
        assert len(schedules) == 1
        assert schedules[0].ci_name == "Valid Row"

    def test_salesforce_ids_parsed(self):
        """Test that Salesforce IDs column is parsed."""
        csv_text = """\
CI Name,CI,Namespace,Users,Enable_workshop_interface,Password,Activity,Purpose,Workshop Name,Provisioning Date (UTC),Auto-stop (UTC),Auto-destroy (UTC),Salesforce IDs
Valid Row,valid-ci,valid-ns,20,True,pass,Admin,QA,My Workshop,15/02/2026 11:00,15/02/2026 19:00,17/02/2026 11:00,71403328
"""
        path = self._write(csv_text)
        schedules = read_csv_input(path)
        assert len(schedules) == 1
        assert schedules[0].salesforce_ids == "71403328"

    def test_count_parsed(self):
        """Test that Count column is parsed."""
        csv_text = """\
CI Name,CI,Namespace,Users,Enable_workshop_interface,Password,Activity,Purpose,Workshop Name,Provisioning Date (UTC),Auto-stop (UTC),Auto-destroy (UTC),Count
Twin Workshop,some-ci,some-ns,20,True,pw,Admin,QA,Twin,15/02/2026 11:00,15/02/2026 19:00,17/02/2026 11:00,2
"""
        path = self._write(csv_text)
        schedules = read_csv_input(path)
        assert len(schedules) == 1
        assert schedules[0].count == 2

    def test_aws_regions_parsed(self):
        """Test that AWS_Region column is parsed."""
        csv_text = """\
CI Name,CI,Namespace,Users,Enable_workshop_interface,Password,Activity,Purpose,Workshop Name,Provisioning Date (UTC),Auto-stop (UTC),Auto-destroy (UTC),AWS_Region
Multi Region,aws.rosa.prod,some-ns,60,True,pw,Admin,QA,ROSA,15/02/2026 11:00,15/02/2026 19:00,17/02/2026 11:00,"us-east-1,eu-west-1"
"""
        path = self._write(csv_text)
        schedules = read_csv_input(path)
        assert len(schedules) == 1
        assert "us-east-1" in schedules[0].aws_regions
        assert "eu-west-1" in schedules[0].aws_regions

    def test_empty_auto_stop_allowed(self):
        """Test that empty Auto-stop (UTC) is allowed."""
        csv_text = """\
CI Name,CI,Namespace,Users,Enable_workshop_interface,Password,Activity,Purpose,Workshop Name,Provisioning Date (UTC),Auto-stop (UTC),Auto-destroy (UTC)
No Stop,some-ci,some-ns,10,True,pw,Admin,QA,No Stop,15/02/2026 11:00,,17/02/2026 11:00
"""
        path = self._write(csv_text)
        schedules = read_csv_input(path)
        assert len(schedules) == 1
        assert schedules[0].auto_stop == ""

    def test_case_insensitive_headers(self):
        """Test that column names are matched case-insensitively."""
        csv_text = """\
ci name,ci,namespace,users,enable_workshop_interface,password,activity,purpose,workshop name,provisioning date (utc),auto-stop (utc),auto-destroy (utc)
Lower Row,lower-ci,lower-ns,5,True,secret,Admin,QA,Lower Workshop,15/02/2026 11:00,15/02/2026 19:00,17/02/2026 11:00
"""
        path = self._write(csv_text)
        schedules = read_csv_input(path)
        assert len(schedules) == 1
        assert schedules[0].ci_name == "Lower Row"
        assert schedules[0].ci == "lower-ci"
        assert schedules[0].namespace == "lower-ns"
        assert schedules[0].users == 5

    def test_read_from_stringio(self):
        """Test that read_csv_input accepts a file-like object (e.g. StringIO)."""
        csv_text = BASIC_WORKSHOP_CSV
        f = io.StringIO(csv_text)
        schedules = read_csv_input(f)
        assert len(schedules) == 1
        assert schedules[0].ci == "openshift-cnv.ocp-virt-roadshow-multi-user.prod"

    def test_salesforce_type_parsed(self):
        """Test that Salesforce_Type column is parsed (campaign, cdh, etc.)."""
        csv_text = """\
CI Name,CI,Namespace,Users,Enable_workshop_interface,Password,Activity,Purpose,Workshop Name,Provisioning Date (UTC),Auto-stop (UTC),Auto-destroy (UTC),Salesforce IDs,Salesforce_Type
Campaign Row,camp-ci,some-ns,20,True,pw,Admin,QA,Campaign,15/02/2026 11:00,15/02/2026 19:00,17/02/2026 11:00,701Pe,campaign
"""
        path = self._write(csv_text)
        schedules = read_csv_input(path)
        assert len(schedules) == 1
        assert schedules[0].salesforce_type == "campaign"
        assert schedules[0].salesforce_ids == "701Pe"

    def test_multiple_rows_same_namespace(self):
        """Test parsing multiple workshops in the same namespace."""
        csv_text = """\
CI Name,CI,Namespace,Users,Enable_workshop_interface,Password,Activity,Purpose,Workshop Name,Provisioning Date (UTC),Auto-stop (UTC),Auto-destroy (UTC)
First Workshop,ci-one,user-team-redhat-com,20,True,pw1,Admin,QA,First,15/02/2026 09:00,15/02/2026 17:00,17/02/2026 09:00
Second Workshop,ci-two,user-team-redhat-com,25,True,pw2,Admin,QA,Second,15/02/2026 10:00,15/02/2026 18:00,17/02/2026 10:00
"""
        path = self._write(csv_text)
        schedules = read_csv_input(path)
        assert len(schedules) == 2
        assert schedules[0].namespace == schedules[1].namespace == "user-team-redhat-com"
        assert schedules[0].ci_name == "First Workshop"
        assert schedules[1].ci_name == "Second Workshop"

    def test_users_optional_empty(self):
        """Test that Users can be empty (optional)."""
        csv_text = """\
CI Name,CI,Namespace,Users,Enable_workshop_interface,Password,Activity,Purpose,Workshop Name,Provisioning Date (UTC),Auto-stop (UTC),Auto-destroy (UTC)
No Users,some-ci,some-ns,,True,pw,Admin,QA,No Users,15/02/2026 11:00,15/02/2026 19:00,17/02/2026 11:00
"""
        path = self._write(csv_text)
        schedules = read_csv_input(path)
        assert len(schedules) == 1
        assert schedules[0].users is None

    def test_example_minimal_workshop_parses(self):
        """Docs example minimal_workshop.csv parses correctly."""
        examples_dir = os.path.join(os.path.dirname(__file__), "..", "docs", "examples")
        path = os.path.join(examples_dir, "minimal_workshop.csv")
        if not os.path.exists(path):
            pytest.skip("docs/examples/minimal_workshop.csv not found")
        schedules = read_csv_input(path)
        assert len(schedules) == 1
        assert schedules[0].ci_name == "Minimal Workshop"
        assert schedules[0].users == 10

    def test_example_count_two_instances_parses(self):
        """Docs example count_two_instances.csv parses Count=2."""
        examples_dir = os.path.join(os.path.dirname(__file__), "..", "docs", "examples")
        path = os.path.join(examples_dir, "count_two_instances.csv")
        if not os.path.exists(path):
            pytest.skip("docs/examples/count_two_instances.csv not found")
        schedules = read_csv_input(path)
        assert len(schedules) == 1
        assert schedules[0].count == 2

    def test_example_no_auto_stop_parses(self):
        """Docs example no_auto_stop.csv has empty auto_stop."""
        examples_dir = os.path.join(os.path.dirname(__file__), "..", "docs", "examples")
        path = os.path.join(examples_dir, "no_auto_stop.csv")
        if not os.path.exists(path):
            pytest.skip("docs/examples/no_auto_stop.csv not found")
        schedules = read_csv_input(path)
        assert len(schedules) == 1
        assert schedules[0].auto_stop == ""

    def test_example_one_workshop_two_regions_parses(self):
        """Docs example one_workshop_two_regions.csv: one workshop across 2 AWS regions."""
        examples_dir = os.path.join(os.path.dirname(__file__), "..", "docs", "examples")
        path = os.path.join(examples_dir, "one_workshop_two_regions.csv")
        if not os.path.exists(path):
            pytest.skip("docs/examples/one_workshop_two_regions.csv not found")
        schedules = read_csv_input(path)
        assert len(schedules) == 1
        assert "us-east-1" in schedules[0].aws_regions
        assert "eu-west-1" in schedules[0].aws_regions

    def test_example_workshop_ui_disabled_parses(self):
        """Docs example workshop_ui_disabled.csv: Enable_workshop_interface False."""
        examples_dir = os.path.join(os.path.dirname(__file__), "..", "docs", "examples")
        path = os.path.join(examples_dir, "workshop_ui_disabled.csv")
        if not os.path.exists(path):
            pytest.skip("docs/examples/workshop_ui_disabled.csv not found")
        schedules = read_csv_input(path)
        assert len(schedules) == 1
        assert schedules[0].enable_workshop_interface is False

    def test_example_single_salesforce_opportunity_parses(self):
        """Docs example single_salesforce_opportunity.csv: one SF opportunity."""
        examples_dir = os.path.join(os.path.dirname(__file__), "..", "docs", "examples")
        path = os.path.join(examples_dir, "single_salesforce_opportunity.csv")
        if not os.path.exists(path):
            pytest.skip("docs/examples/single_salesforce_opportunity.csv not found")
        schedules = read_csv_input(path)
        assert len(schedules) == 1
        assert schedules[0].salesforce_ids == "71456169"
        assert schedules[0].salesforce_type == "opportunity"

    def test_example_users_omitted_parses(self):
        """Docs example users_omitted.csv: Users column empty."""
        examples_dir = os.path.join(os.path.dirname(__file__), "..", "docs", "examples")
        path = os.path.join(examples_dir, "users_omitted.csv")
        if not os.path.exists(path):
            pytest.skip("docs/examples/users_omitted.csv not found")
        schedules = read_csv_input(path)
        assert len(schedules) == 1
        assert schedules[0].users is None

    def test_example_multi_asset_with_instances_parses(self):
        """Docs example multi_asset_with_instances.csv: Instances and Concurrency."""
        examples_dir = os.path.join(os.path.dirname(__file__), "..", "docs", "examples")
        path = os.path.join(examples_dir, "multi_asset_with_instances.csv")
        if not os.path.exists(path):
            pytest.skip("docs/examples/multi_asset_with_instances.csv not found")
        schedules = read_csv_input(path)
        assert len(schedules) == 2
        assert schedules[0].instances == 30
        assert schedules[0].concurrency == 2
        assert schedules[0].multi_workshop_name == "summit-30-seats"

    def test_redirect_column_false(self):
        """Test that Redirect=False sets schedule.redirect to False."""
        csv_text = """\
CI Name,CI,Namespace,Users,Enable_workshop_interface,Password,Activity,Purpose,Workshop Name,Provisioning Date (UTC),Auto-stop (UTC),Auto-destroy (UTC),Redirect
No Redirect,some-ci,some-ns,20,True,pw,Admin,QA,No Redir,15/02/2026 11:00,15/02/2026 19:00,17/02/2026 11:00,False
"""
        path = self._write(csv_text)
        schedules = read_csv_input(path)
        assert len(schedules) == 1
        assert schedules[0].redirect is False

    def test_redirect_column_missing_defaults_true(self):
        """Test that missing Redirect column defaults to True."""
        csv_text = """\
CI Name,CI,Namespace,Users,Enable_workshop_interface,Password,Activity,Purpose,Workshop Name,Provisioning Date (UTC),Auto-stop (UTC),Auto-destroy (UTC)
Default Redirect,some-ci,some-ns,20,True,pw,Admin,QA,Default,15/02/2026 11:00,15/02/2026 19:00,17/02/2026 11:00
"""
        path = self._write(csv_text)
        schedules = read_csv_input(path)
        assert len(schedules) == 1
        assert schedules[0].redirect is True

    def test_archive_column_ignored(self):
        """Archive column is ignored: all rows are read regardless of Archive value or empty."""
        csv_text = """\
CI Name,CI,Namespace,Users,Enable_workshop_interface,Password,Activity,Purpose,Archive,Workshop Name,Provisioning Date (UTC),Auto-stop (UTC),Auto-destroy (UTC)
First,ci-one,ns-a,20,True,pw,Admin,QA,Archive,First,15/02/2026 11:00,15/02/2026 19:00,17/02/2026 11:00
Second,ci-two,ns-b,10,True,pw,Admin,QA,,Second,15/02/2026 11:00,15/02/2026 19:00,17/02/2026 11:00
"""
        path = self._write(csv_text)
        schedules = read_csv_input(path)
        assert len(schedules) == 2
        assert schedules[0].ci_name == "First" and schedules[0].ci == "ci-one"
        assert schedules[1].ci_name == "Second" and schedules[1].ci == "ci-two"

    def test_example_multi_asset_companion_parses(self):
        """Docs example multi_asset_companion.csv: grouped multi-asset with companion passwords."""
        examples_dir = os.path.join(os.path.dirname(__file__), "..", "docs", "examples")
        path = os.path.join(examples_dir, "multi_asset_companion.csv")
        if not os.path.exists(path):
            pytest.skip("docs/examples/multi_asset_companion.csv not found")
        schedules = read_csv_input(path)
        assert len(schedules) == 2
        assert schedules[0].multi_workshop_name == "summit-demo-2026"


class TestLoadAssetPasswords:
    """Tests for load_asset_passwords."""

    def setup_method(self):
        self._tmpfiles = []

    def teardown_method(self):
        for f in self._tmpfiles:
            try:
                os.unlink(f)
            except OSError:
                pass

    def _write(self, csv_text):
        path = write_csv_tempfile(csv_text)
        self._tmpfiles.append(path)
        return path

    def test_load_asset_passwords_missing_file(self):
        """Non-existent file returns empty dict."""
        result = load_asset_passwords("/nonexistent/passwords.csv")
        assert result == {}

    def test_load_asset_passwords_none_path(self):
        """None path returns empty dict."""
        result = load_asset_passwords(None)
        assert result == {}

    def test_load_asset_passwords_ci_password_columns(self):
        """CI and Password columns are parsed."""
        csv_text = "CI,Password\nci-a,pass-a\nci-b,pass-b\n"
        path = self._write(csv_text)
        result = load_asset_passwords(path)
        assert result["ci-a"] == "pass-a"
        assert result["ci-b"] == "pass-b"

    def test_load_asset_passwords_skips_empty_rows(self):
        """Rows with missing CI or Password are skipped."""
        csv_text = "CI,Password\nci-a,pass-a\n,\nci-b,\n\nci-c,pass-c\n"
        path = self._write(csv_text)
        result = load_asset_passwords(path)
        assert result.get("ci-a") == "pass-a"
        assert result.get("ci-c") == "pass-c"
        assert "ci-b" not in result


class TestLoadAssetNumUsers:
    """Tests for load_asset_num_users."""

    def setup_method(self):
        self._tmpfiles = []

    def teardown_method(self):
        for f in self._tmpfiles:
            try:
                os.unlink(f)
            except OSError:
                pass

    def _write(self, csv_text):
        path = write_csv_tempfile(csv_text)
        self._tmpfiles.append(path)
        return path

    def test_load_asset_num_users_missing_file(self):
        """Non-existent file returns empty dict."""
        result = load_asset_num_users("/nonexistent/asset_users.csv")
        assert result == {}

    def test_load_asset_num_users_ci_num_users_columns(self):
        """CI and num_users (or Users) columns are parsed."""
        csv_text = "CI,num_users\nci-a,10\nci-b,20\n"
        path = self._write(csv_text)
        result = load_asset_num_users(path)
        assert result["ci-a"] == 10
        assert result["ci-b"] == 20

    def test_load_asset_num_users_users_column_fallback(self):
        """Users column is used when num_users is not present."""
        csv_text = "CI,Users\nci-x,30\n"
        path = self._write(csv_text)
        result = load_asset_num_users(path)
        assert result["ci-x"] == 30

    def test_load_asset_num_users_invalid_number_skipped(self):
        """Invalid number in num_users is skipped (no crash)."""
        csv_text = "CI,num_users\nci-a,10\nci-b,not-a-number\nci-c,15\n"
        path = self._write(csv_text)
        result = load_asset_num_users(path)
        assert result["ci-a"] == 10
        assert result["ci-c"] == 15
        assert "ci-b" not in result


def test_read_csv_parses_event_id(tmp_path):
    from rhdp_flow import read_csv_input
    csv_path = tmp_path / "sched.csv"
    csv_path.write_text(
        "CI Name,CI,Namespace,Users,Enable_workshop_interface,Password,Activity,Purpose,"
        "Workshop Name,Provisioning Date (UTC),Auto-stop (UTC),Auto-destroy (UTC),Event_Id\n"
        "Lab,lab.prod,user-ns,1,true,pw,Admin,QA,Lab,01/08/2026 10:00,01/08/2026 18:00,02/08/2026 10:00,32\n"
    )
    schedules = read_csv_input(str(csv_path))
    assert len(schedules) == 1
    assert schedules[0].event_id == "32"
