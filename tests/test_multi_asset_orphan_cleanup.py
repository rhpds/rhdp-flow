"""Safe cleanup of Flow-precreated multi-asset Workshop shells."""

from __future__ import annotations

import json
import subprocess
import unittest
from unittest.mock import patch

from rhdp_flow import (
    _MULTI_ASSET_SHELL_CLEANUP_POLLS,
    cleanup_multi_asset_shell_workshops,
)
from tests.test_rhdp_flow import make_config


class TestCleanupMultiAssetShellWorkshops(unittest.TestCase):
    def test_dry_run_skips_deletes(self):
        config = make_config(dry_run=True)
        deleted = cleanup_multi_asset_shell_workshops(
            ["shell-a", "shell-b"], "mw-1", "user-test", config
        )
        self.assertEqual(deleted, [])

    @patch("rhdp_flow.subprocess.run")
    def test_keeps_shells_still_listed_on_mw(self, mock_run):
        """If controller has not rewritten assets, leave Flow shells alone."""
        config = make_config(dry_run=False)

        def dispatcher(*args, **kwargs):
            cmd = args[0]
            if cmd[1] == "get" and cmd[2] == "multiworkshop":
                payload = {
                    "spec": {
                        "assets": [
                            {"name": "shell-a"},
                            {"name": "shell-b"},
                        ]
                    }
                }
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=json.dumps(payload), stderr=""
                )
            if cmd[1] == "delete":
                self.fail(f"must not delete while still referenced: {cmd}")
            return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")

        mock_run.side_effect = dispatcher
        deleted = cleanup_multi_asset_shell_workshops(
            ["shell-a", "shell-b"], "mw-1", "user-test", config, only_if_replaced=True
        )
        self.assertEqual(deleted, [])

    @patch("rhdp_flow.subprocess.run")
    def test_deletes_only_unreferenced_unprotected_shells(self, mock_run):
        config = make_config(dry_run=False)
        deleted_names: list[str] = []

        def dispatcher(*args, **kwargs):
            cmd = args[0]
            if cmd[1] == "get" and cmd[2] == "multiworkshop":
                # Controller replaced shells with owned workshops
                payload = {
                    "spec": {
                        "assets": [
                            {"name": "owned-a"},
                            {"name": "owned-b"},
                        ]
                    }
                }
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=json.dumps(payload), stderr=""
                )
            if cmd[1] == "get" and cmd[2] == "workshop":
                name = cmd[3]
                if name == "shell-protected":
                    payload = {
                        "metadata": {
                            "labels": {
                                "babylon.gpte.redhat.com/multiworkshop": "mw-1"
                            }
                        }
                    }
                else:
                    payload = {"metadata": {"labels": {}, "ownerReferences": []}}
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=json.dumps(payload), stderr=""
                )
            if cmd[1] == "delete" and cmd[2] == "workshop":
                deleted_names.append(cmd[3])
                return subprocess.CompletedProcess(cmd, 0, stdout="deleted", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")

        mock_run.side_effect = dispatcher
        deleted = cleanup_multi_asset_shell_workshops(
            ["shell-a", "shell-protected", "shell-b"],
            "mw-1",
            "user-test",
            config,
            only_if_replaced=True,
        )
        self.assertEqual(sorted(deleted), ["shell-a", "shell-b"])
        self.assertEqual(sorted(deleted_names), ["shell-a", "shell-b"])
        self.assertNotIn("shell-protected", deleted_names)

    @patch("rhdp_flow.subprocess.run")
    def test_fail_closed_when_workshop_inspect_errors(self, mock_run):
        config = make_config(dry_run=False)

        def dispatcher(*args, **kwargs):
            cmd = args[0]
            if cmd[1] == "get" and cmd[2] == "multiworkshop":
                payload = {"spec": {"assets": [{"name": "owned-a"}]}}
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=json.dumps(payload), stderr=""
                )
            if cmd[1] == "get" and cmd[2] == "workshop":
                # Simulate API flake — cleanup must not delete
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="timeout")
            if cmd[1] == "delete":
                self.fail("must not delete when inspect fails")
            return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")

        mock_run.side_effect = dispatcher
        deleted = cleanup_multi_asset_shell_workshops(
            ["shell-a"], "mw-1", "user-test", config, only_if_replaced=True
        )
        self.assertEqual(deleted, [])

    def test_poll_constant_is_tuple(self):
        self.assertIsInstance(_MULTI_ASSET_SHELL_CLEANUP_POLLS, tuple)
        self.assertEqual(len(_MULTI_ASSET_SHELL_CLEANUP_POLLS), 2)


if __name__ == "__main__":
    unittest.main()
