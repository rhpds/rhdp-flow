"""Conservative parent deadline checks, shared with Flow. No resource mutations."""

import re
from datetime import UTC, datetime, timedelta


def timestamp(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError
        return result.astimezone(UTC)
    except (ValueError, TypeError, AttributeError):
        raise ValueError("parent_lifetime_invalid_timestamp") from None


def duration(value):
    # Poolboy allows templated durations. Never evaluate those locally or assume
    # their result; only a concrete, positive duration can pass this preflight.
    if not isinstance(value, str) or not re.fullmatch(r"(?:\d+(?:\.\d+)?[smhdw])+", value):
        raise ValueError("parent_lifetime_policy_unresolved")
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    try:
        result = timedelta(seconds=sum(float(n) * units[u] for n, u in re.findall(r"(\d+(?:\.\d+)?)([smhdw])", value)))
    except (ValueError, OverflowError):
        raise ValueError("parent_lifetime_policy_unresolved") from None
    if result <= timedelta(0):
        raise ValueError("parent_lifetime_policy_unresolved")
    return result


def validate_policy(spec, start, end, *, now=None):
    """Defaults are overridable; maxima bound the requested deadline."""
    lifespan = spec.get("lifespan") or {}
    if not isinstance(lifespan, dict) or spec.get("autoDetach") or spec.get("autoDelete"):
        raise ValueError("parent_lifetime_policy_unresolved")
    start, end = timestamp(start), timestamp(end)
    now = now or datetime.now(UTC)
    if end <= start:
        raise ValueError("parent_lifetime_invalid_timestamp")
    for field in ("maximum", "relativeMaximum"):
        value = lifespan.get(field)
        if value is not None:
            limit = duration(value)
            base = start if field == "maximum" else max(now, start)
            if end - base > limit:
                raise ValueError("parent_lifetime_exceeds_policy")


def effective_deadline_verified(claim, handle, end):
    """Both controller-reported claim and actual handle must cover the plan.

    Missing controller acknowledgement is pending, never evidence of infinity.
    A shorter accepted deadline blocks children, even when the request was long
    enough. Check again before every child submission and during reconciliation.
    """
    required = timestamp(end)
    accepted = [
        (claim.get("status") or {}).get("lifespan", {}).get("end"),
        (handle.get("spec") or {}).get("lifespan", {}).get("end"),
    ]
    for value in accepted:
        if value is not None and timestamp(value) < required:
            raise ValueError("parent_lifetime_shorter_than_required")
    requested = (claim.get("spec") or {}).get("lifespan", {}).get("end")
    if requested is not None and timestamp(requested) < required:
        raise ValueError("parent_lifetime_shorter_than_required")
    return all(value is not None for value in accepted)
