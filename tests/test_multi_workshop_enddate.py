"""MultiWorkshop endDate must follow CSV Auto-destroy (not start+24h)."""

from __future__ import annotations

import json
import subprocess
import unittest
from unittest.mock import patch

from rhdp_flow import (
    _ensure_multiworkshop_schedule_dates,
    create_multi_workshop,
    format_iso8601,
    parse_date_time,
)
from tests.conftest import make_config, make_oc_dispatcher, make_schedule


class TestEnsureMultiWorkshopScheduleDates(unittest.TestCase):
    @patch("rhdp_flow.subprocess.run")
    def test_no_patch_when_dates_match(self, mock_run):
        config = make_config(dry_run=False)
        start = "2026-11-24T22:00:00Z"
        end = "2026-11-25T01:00:00Z"

        def dispatcher(*args, **kwargs):
            cmd = args[0]
            if cmd[1] == "get" and cmd[2] == "multiworkshop":
                payload = {"spec": {"startDate": start, "endDate": end}}
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=json.dumps(payload), stderr=""
                )
            if cmd[1] == "patch":
                self.fail(f"must not patch when dates match: {cmd}")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        mock_run.side_effect = dispatcher
        ok = _ensure_multiworkshop_schedule_dates(
            "automation-test", "event-ns", start, end, config
        )
        self.assertTrue(ok)

    @patch("rhdp_flow.subprocess.run")
    def test_patches_start_plus_24h_enddate_to_auto_destroy(self, mock_run):
        """Live bug shape: endDate landed as start+24h despite Auto-destroy."""
        config = make_config(dry_run=False)
        start = "2026-11-24T22:00:00Z"
        expected_end = "2026-11-25T01:00:00Z"
        wrong_end = "2026-11-25T22:00:00Z"
        patched: list[dict] = []
        get_calls = {"n": 0}

        def dispatcher(*args, **kwargs):
            cmd = args[0]
            if cmd[1] == "get" and cmd[2] == "multiworkshop":
                get_calls["n"] += 1
                # First get: wrong endDate. jsonpath verify after patch: correct.
                if any("jsonpath" in str(a) for a in cmd):
                    return subprocess.CompletedProcess(
                        cmd, 0, stdout=f"{start} {expected_end}", stderr=""
                    )
                payload = {"spec": {"startDate": start, "endDate": wrong_end}}
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=json.dumps(payload), stderr=""
                )
            if cmd[1] == "patch" and cmd[2] == "multiworkshop":
                for i, arg in enumerate(cmd):
                    if arg == "-p" and i + 1 < len(cmd):
                        patched.append(json.loads(cmd[i + 1]))
                return subprocess.CompletedProcess(cmd, 0, stdout="patched", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        mock_run.side_effect = dispatcher
        ok = _ensure_multiworkshop_schedule_dates(
            "automation-2t1z4", "event-42-25112026-1600", start, expected_end, config
        )
        self.assertTrue(ok)
        self.assertEqual(len(patched), 1)
        self.assertEqual(
            patched[0],
            {"spec": {"startDate": start, "endDate": expected_end}},
        )


class TestCreateMultiWorkshopEndDate(unittest.TestCase):
    @patch("rhdp_flow._MULTI_ASSET_SHELL_CLEANUP_POLLS", (1, 0))
    @patch("rhdp_flow.get_catalog_item_info", return_value={
        "namespace": "babylon-catalog-event",
        "displayName": "Asset A",
    })
    @patch("rhdp_flow.subprocess.run")
    def test_create_payload_enddate_is_auto_destroy_and_ensures_dates(
        self, mock_run, _catalog
    ):
        """Create manifest endDate == Auto-destroy; ensure runs after create."""
        config = make_config(dry_run=False)
        schedule = make_schedule(
            is_multi_asset=True,
            asset_cis="ci1.event",
            multi_workshop_name="maw-enddate-test",
            provisioning_date="24/11/2026 22:00",
            auto_stop="",
            auto_destroy="25/11/2026 01:00",
        )
        expected_start = format_iso8601(parse_date_time(schedule.provisioning_date))
        expected_end = format_iso8601(parse_date_time(schedule.auto_destroy))
        self.assertEqual(expected_end, "2026-11-25T01:00:00Z")

        created_mws: list[dict] = []
        ensure_gets = {"n": 0}
        base = make_oc_dispatcher()

        def dispatcher(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if len(cmd) >= 2 and cmd[1] == "create":
                for i, arg in enumerate(cmd):
                    if arg == "-f" and i + 1 < len(cmd):
                        with open(cmd[i + 1]) as f:
                            payload = json.load(f)
                        if payload.get("kind") == "MultiWorkshop":
                            created_mws.append(payload)
            if (
                len(cmd) >= 3
                and cmd[1] == "get"
                and cmd[2] == "multiworkshop"
                and any(a == "json" for a in cmd)
            ):
                ensure_gets["n"] += 1
                # Simulate cluster keeping Auto-destroy endDate.
                name = cmd[3] if len(cmd) > 3 else "maw-enddate-test"
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    stdout=json.dumps({
                        "metadata": {"name": name},
                        "spec": {
                            "startDate": expected_start,
                            "endDate": expected_end,
                            "assets": [{"name": "owned-a"}],
                        },
                    }),
                    stderr="",
                )
            return base(*args, **kwargs)

        mock_run.side_effect = dispatcher
        name = create_multi_workshop(schedule, config)
        self.assertEqual(name, "maw-enddate-test")
        self.assertEqual(len(created_mws), 1)
        spec = created_mws[0]["spec"]
        self.assertEqual(spec["startDate"], expected_start)
        self.assertEqual(spec["endDate"], expected_end)
        # Must not be start+24h
        self.assertNotEqual(spec["endDate"], "2026-11-25T22:00:00Z")
        self.assertGreaterEqual(ensure_gets["n"], 1)

    @patch("rhdp_flow._MULTI_ASSET_SHELL_CLEANUP_POLLS", (1, 0))
    @patch("rhdp_flow.get_catalog_item_info", return_value={
        "namespace": "babylon-catalog-event",
        "displayName": "Asset A",
    })
    @patch("rhdp_flow.subprocess.run")
    def test_already_exists_patches_wrong_enddate(self, mock_run, _catalog):
        config = make_config(dry_run=False)
        schedule = make_schedule(
            is_multi_asset=True,
            asset_cis="ci1.event",
            multi_workshop_name="maw-exists",
            provisioning_date="24/11/2026 22:00",
            auto_stop="",
            auto_destroy="25/11/2026 01:00",
        )
        expected_start = "2026-11-24T22:00:00Z"
        expected_end = "2026-11-25T01:00:00Z"
        wrong_end = "2026-11-25T22:00:00Z"
        patches: list[dict] = []
        base = make_oc_dispatcher()

        def dispatcher(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if len(cmd) >= 2 and cmd[1] == "create":
                # Only fail MultiWorkshop create as already-exists; workshops succeed.
                tmpfile = None
                for i, arg in enumerate(cmd):
                    if arg == "-f" and i + 1 < len(cmd):
                        tmpfile = cmd[i + 1]
                        break
                kind = ""
                if tmpfile:
                    with open(tmpfile) as f:
                        kind = json.load(f).get("kind", "")
                if kind == "MultiWorkshop":
                    return subprocess.CompletedProcess(
                        cmd,
                        1,
                        stdout="",
                        stderr='Error from server (AlreadyExists): multiworkshops.babylon.gpte.redhat.com "maw-exists" already exists',
                    )
            if len(cmd) >= 3 and cmd[1] == "get" and cmd[2] == "multiworkshop":
                if any("jsonpath" in str(a) for a in cmd):
                    return subprocess.CompletedProcess(
                        cmd, 0, stdout=f"{expected_start} {expected_end}", stderr=""
                    )
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    stdout=json.dumps({
                        "spec": {
                            "startDate": expected_start,
                            "endDate": wrong_end,
                            "assets": [{"name": "owned-a"}],
                        }
                    }),
                    stderr="",
                )
            if len(cmd) >= 3 and cmd[1] == "patch" and cmd[2] == "multiworkshop":
                for i, arg in enumerate(cmd):
                    if arg == "-p" and i + 1 < len(cmd):
                        patches.append(json.loads(cmd[i + 1]))
                return subprocess.CompletedProcess(cmd, 0, stdout="patched", stderr="")
            return base(*args, **kwargs)

        mock_run.side_effect = dispatcher
        name = create_multi_workshop(schedule, config)
        self.assertEqual(name, "maw-exists")
        self.assertEqual(len(patches), 1)
        self.assertEqual(
            patches[0]["spec"]["endDate"],
            expected_end,
        )


if __name__ == "__main__":
    unittest.main()
