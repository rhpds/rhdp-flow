from datetime import UTC, datetime

import pytest

from lib.dependency_lifetime import effective_deadline_verified, validate_policy

START = "2026-11-18T10:00:00Z"
END = "2026-11-21T10:00:00Z"
NOW = datetime(2026, 11, 18, 10, tzinfo=UTC)


def test_defaults_can_be_overridden_within_maxima():
    validate_policy({"lifespan": {"default": "48h", "maximum": "14d", "relativeMaximum": "5d"}}, START, END, now=NOW)


@pytest.mark.parametrize("policy", [{"maximum": "2d"}, {"relativeMaximum": "2d"}])
def test_short_maxima_rejected(policy):
    with pytest.raises(ValueError, match="parent_lifetime_exceeds_policy"):
        validate_policy({"lifespan": policy}, START, END, now=NOW)


@pytest.mark.parametrize("value", ["{{ duration }}", "forever", "0d", -1, {}, "1dgarbage"])
def test_unknown_policy_is_not_assumed_safe(value):
    with pytest.raises(ValueError, match="policy_unresolved"):
        validate_policy({"lifespan": {"maximum": value}}, START, END, now=NOW)


def test_effective_deadlines_must_both_be_acknowledged():
    claim = {"spec": {"lifespan": {"end": END}}, "status": {}}
    handle = {"spec": {"lifespan": {"end": END}}}
    assert not effective_deadline_verified(claim, handle, END)
    claim["status"]["lifespan"] = {"end": END}
    assert effective_deadline_verified(claim, handle, END)
    handle["spec"]["lifespan"]["end"] = START
    with pytest.raises(ValueError, match="shorter_than_required"):
        effective_deadline_verified(claim, handle, END)


def test_naive_timestamp_rejected():
    with pytest.raises(ValueError, match="invalid_timestamp"):
        validate_policy({}, START, "2026-11-21T10:00:00", now=NOW)
