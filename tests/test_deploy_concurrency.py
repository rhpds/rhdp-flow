"""Parallel deploy loop respects concurrency limit."""

from __future__ import annotations

import asyncio
import threading
import time
from unittest.mock import MagicMock

from api import routes
from rhdp_flow import DeploymentResult, WorkshopSchedule


def _sched(i: int) -> WorkshopSchedule:
    return WorkshopSchedule(
        ci_name=f"lab-{i}",
        ci=f"ci-{i}.prod",
        namespace="event-42",
        enable_workshop_interface=True,
        password="x",
        activity="Admin",
        purpose="QA",
        workshop_name=f"Lab {i}",
        provisioning_date="",
        auto_stop="",
        auto_destroy="",
        users=1,
        is_multi_asset=False,
        multi_workshop_name="",
    )


def test_run_deploy_over_limits_inflight(monkeypatch):
    peak_box = {"inflight": 0, "peak": 0}
    peak_lock = threading.Lock()

    def fake_process(sched, config, asset_passwords=None):
        with peak_lock:
            peak_box["inflight"] += 1
            peak_box["peak"] = max(peak_box["peak"], peak_box["inflight"])
        time.sleep(0.05)
        with peak_lock:
            peak_box["inflight"] -= 1
        return DeploymentResult(
            ci_name=sched.ci_name,
            ci=sched.ci,
            namespace=sched.namespace,
            guid="g",
            url="u",
            status="deployed_unverified",
            provisioning_date="",
            auto_stop="",
            auto_destroy="",
            timestamp="",
        )

    async def _noop_wait(job_id):
        return None

    monkeypatch.setattr(routes, "process_schedule", fake_process)
    monkeypatch.setattr(routes, "_ensure_event_namespaces_for_schedules", lambda *a, **k: None)
    monkeypatch.setattr(routes.jobs, "wait_if_paused", _noop_wait)
    monkeypatch.setattr(routes.jobs, "is_cancel_requested", lambda job_id: False)
    monkeypatch.setattr(routes.jobs, "update_job", lambda *a, **k: None)

    config = MagicMock()
    config.dry_run = True
    config.base_domain = "example.com"

    schedules = [_sched(i) for i in range(8)]

    async def exercise():
        return await routes._run_deploy_over(
            schedules, config, "job-1", None, pace_seconds=0, concurrency=3
        )

    results = asyncio.run(exercise())

    assert len(results) == 8
    assert peak_box["peak"] <= 3
    assert peak_box["peak"] >= 2
