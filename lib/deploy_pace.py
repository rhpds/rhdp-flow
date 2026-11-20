"""Deploy pacing and concurrency helpers (ease API / admission pressure)."""

from __future__ import annotations

import os

_DEFAULT_CONCURRENCY = 5
_MAX_CONCURRENCY = 20


def deploy_pace_seconds(batch_size: int, override: float | None = None) -> float:
    """Seconds to wait between workshops after each create (serial mode only).

    Explicit ``override`` wins (clamped to >= 0). Otherwise scale with batch size.

    Defaults are intentionally light — Flow only schedules Workshop /
    WorkshopProvision creates via the API; it does not wait for VMs to provision.
    Heavy pauses (3–5s) made Summit-sized batches feel like real deploys.

    Auto (when override is None):
    - < 10 workshops → 0s
    - 10–24 → 0.5s
    - 25+ → 1s

    When ``deploy_concurrency`` > 1, callers skip this pause; the semaphore
    already bounds in-flight creates.
    """
    if override is not None:
        return max(0.0, float(override))
    n = max(0, int(batch_size))
    if n >= 25:
        return 1.0
    if n >= 10:
        return 0.5
    return 0.0


def deploy_concurrency(override: int | None = None) -> int:
    """Max parallel workshop/multi-workshop creates during deploy.

    Explicit ``override`` wins (clamped to 1..20). Else ``RHDP_DEPLOY_CONCURRENCY``
    env, else 5.
    """
    if override is not None:
        return max(1, min(_MAX_CONCURRENCY, int(override)))
    raw = os.environ.get("RHDP_DEPLOY_CONCURRENCY", "").strip()
    if raw:
        try:
            return max(1, min(_MAX_CONCURRENCY, int(raw)))
        except ValueError:
            pass
    return _DEFAULT_CONCURRENCY
