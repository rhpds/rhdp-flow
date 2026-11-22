"""Unit tests for SelfPacedLab deploy helpers."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from lib.selfpacedlab_deploy import (
    build_provision_item_dict,
    build_selfpacedlab_dict,
    desired_pool_action,
    is_selfpaced_schedule,
    stable_selfpacedlab_name,
)


def _sched(**kw):
    base = dict(
        ci_name="LB1063",
        ci="summit.lb1063.event",
        namespace="user-ns",
        enable_workshop_interface=True,
        password="",
        activity="Admin",
        purpose="QA",
        workshop_name="SP Lab",
        provisioning_date="24/11/2026 01:00",
        auto_stop="24/11/2026 08:00",
        auto_destroy="",
        session_date="2026-11-24",
        event_id="42",
        lab_code="LB1063",
        instances=5,
        item_type="selfpacedlab",
        catalog_namespace="babylon-catalog-event",
        white_glove=True,
        concurrency=5,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_is_selfpaced_schedule_variants():
    assert is_selfpaced_schedule(_sched(item_type="selfpacedlab"))
    assert is_selfpaced_schedule(_sched(item_type="Self-Paced"))
    assert is_selfpaced_schedule(_sched(item_type="self_paced"))
    assert not is_selfpaced_schedule(_sched(item_type="workshop"))
    assert not is_selfpaced_schedule(_sched(item_type="tenant"))


def test_stable_name_includes_event_and_lab():
    assert stable_selfpacedlab_name(_sched()) == "spl-42-lb1063"


def test_desired_pool_scale_up_during_window():
    now = datetime(2026, 11, 24, 3, 0, tzinfo=timezone.utc)
    size, action = desired_pool_action(_sched(), now=now)
    assert action == "scale_up"
    assert size == 5


def test_desired_pool_flush_after_auto_stop():
    now = datetime(2026, 11, 24, 9, 0, tzinfo=timezone.utc)
    size, action = desired_pool_action(_sched(), now=now)
    assert action == "flush"
    assert size == 0


def test_desired_pool_defer_before_provision():
    now = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
    size, action = desired_pool_action(_sched(), now=now)
    assert action == "defer"
    assert size is None


def test_build_spl_and_provision_dicts():
    with patch("lib.selfpacedlab_deploy.catalog_namespace_for", return_value="babylon-catalog-event"):
        spl = build_selfpacedlab_dict(
            _sched(auto_destroy="26/11/2026 13:30"),
            config=SimpleNamespace(),
            name="spl-42-lb1063",
            lifespan_start="2026-11-24T01:00:00Z",
            lifespan_end="2026-11-26T13:30:00Z",
        )
        assert spl["kind"] == "SelfPacedLab"
        assert spl["spec"]["lifespan"]["end"] == "2026-11-26T13:30:00Z"
        assert spl["metadata"]["labels"]["labagator.redhat.com/event-id"] == "42"
        spi = build_provision_item_dict(
            _sched(),
            config=SimpleNamespace(),
            name="spl-42-lb1063",
            spl_uid="uid-1",
            pool_size=5,
        )
        assert spi["kind"] == "SelfPacedLabProvisionItem"
        assert spi["spec"]["poolSize"] == 5
        assert spi["spec"]["catalogItem"]["name"] == "summit.lb1063.event"
