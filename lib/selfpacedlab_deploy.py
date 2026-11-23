"""SelfPacedLab QA helpers for Flow (read-only).

Labagator owns SelfPacedLab create / scale / flush. Flow only detects
self-paced CSV rows for QA2/QA3 against pools Labagator manages.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

BABYLON_GROUP = "babylon.gpte.redhat.com"
SPL_PLURAL = "selfpacedlabs"
SPI_PLURAL = "selfpacedlabprovisionitems"


def is_selfpaced_schedule(schedule) -> bool:
    """True when CSV Item_Type selects a SelfPacedLab row (Labagator-managed)."""
    raw = (getattr(schedule, "item_type", None) or "").strip().lower().replace("_", "").replace("-", "")
    return raw in {"selfpacedlab", "selfpaced", "spl"}


def stable_selfpacedlab_name(schedule) -> str:
    """Deterministic SelfPacedLab name from event-id + lab-code (or CI)."""
    event = (getattr(schedule, "event_id", None) or "").strip() or "evt"
    code = (getattr(schedule, "lab_code", None) or "").strip()
    if not code:
        ci = (getattr(schedule, "ci", None) or getattr(schedule, "ci_name", None) or "lab").strip()
        code = ci.split(".")[0]
    base = f"spl-{event}-{code}".lower()
    base = re.sub(r"[^a-z0-9.-]+", "-", base)
    base = re.sub(r"-+", "-", base).strip("-.")
    return base[:63] or "spl-lab"


def _oc_env(config) -> dict:
    env = os.environ.copy()
    if getattr(config, "kubeconfig_path", None):
        env["KUBECONFIG"] = config.kubeconfig_path
    return env


def _parse_dt(value: str | None):
    from rhdp_flow import parse_date_time

    if not value:
        return None
    return parse_date_time(value, assume_utc=True)


def desired_pool_action(schedule, *, now: datetime | None = None) -> tuple[Optional[int], str]:
    """Expected pool action from CSV window (for QA expectations only).

    - scale_up → Instances expected as poolSize
    - flush → poolSize 0 after Auto-stop
    - defer → before provisioning; do not assert poolSize
    """
    now = now or datetime.now(timezone.utc)
    n = getattr(schedule, "instances", None)
    if n is None or int(n) < 0:
        n = getattr(schedule, "users", None) or 0
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = 0

    stop = _parse_dt(getattr(schedule, "auto_stop", None))
    prov = _parse_dt(getattr(schedule, "provisioning_date", None))

    if stop and now >= stop:
        return 0, "flush"
    if prov and now < prov:
        return None, "defer"
    return max(0, n), "scale_up"


def _oc_get_json(config, plural: str, name: str, namespace: str) -> dict | None:
    cmd = [
        config.oc_command,
        "get",
        f"{plural}.{BABYLON_GROUP}",
        name,
        "-n",
        namespace,
        "-o",
        "json",
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=getattr(config, "timeout", 60),
        env=_oc_env(config),
    )
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def qa_selfpaced_setup_result(schedule, namespace: str, config) -> dict:
    """QA2: SelfPacedLab + ProvisionItem exist; poolSize / lifespan sane."""
    name = stable_selfpacedlab_name(schedule)
    ns = namespace or schedule.namespace
    spl = _oc_get_json(config, SPL_PLURAL, name, ns)
    spi = _oc_get_json(config, SPI_PLURAL, name, ns)
    issues = []
    if not spl:
        issues.append("SelfPacedLab not found")
    if not spi:
        issues.append("SelfPacedLabProvisionItem not found")

    expected = getattr(schedule, "instances", None) or 0
    try:
        expected = int(expected)
    except (TypeError, ValueError):
        expected = 0
    actual_pool = None
    if spi:
        actual_pool = (spi.get("spec") or {}).get("poolSize")
        _, action = desired_pool_action(schedule)
        if action == "flush" and actual_pool not in (0, "0", None):
            if int(actual_pool or 0) > 0:
                issues.append(f"poolSize={actual_pool} expected 0 (flushed)")
        elif action == "scale_up" and expected and int(actual_pool or 0) != expected:
            issues.append(f"poolSize={actual_pool} expected {expected}")

    life = ((spl or {}).get("spec") or {}).get("lifespan") or {}
    healthy = not issues
    return {
        "ci_name": schedule.ci_name,
        "ci": schedule.ci,
        "namespace": ns,
        "scheduled": "Yes",
        "deployed": "Yes" if spl and spi else "No",
        "status": "✅ SELFPACEDLAB OK" if healthy else "❌ SELFPACEDLAB SETUP",
        "expected_seats": expected,
        "actual_seats": actual_pool,
        "workshop_users_assigned": "",
        "total_seats": actual_pool,
        "seats_match": "Yes" if healthy else "No",
        "healthy": healthy,
        "ready": bool(spl and spi),
        "provisioned": bool(spi),
        "lock_status": None,
        "provisioning_date": schedule.provisioning_date,
        "auto_stop": schedule.auto_stop,
        "auto_destroy": schedule.auto_destroy,
        "actual_start": life.get("start", ""),
        "actual_destroy": life.get("end", ""),
        "issues": "; ".join(issues),
        "resourceclaim_name": name,
        "link_to_service": f"/selfpacedlabs/{ns}/{name}",
        "landing_page_url": "",
        "item_type": "selfpacedlab",
        "skip_soundcheck": True,
    }


def qa_selfpaced_health_result(schedule, namespace: str, config) -> dict:
    """QA3: poolCount healthy vs poolSize; skip Soundcheck."""
    name = stable_selfpacedlab_name(schedule)
    ns = namespace or schedule.namespace
    spl = _oc_get_json(config, SPL_PLURAL, name, ns)
    spi = _oc_get_json(config, SPI_PLURAL, name, ns)
    issues = []
    if not spl:
        issues.append("SelfPacedLab not found")
    if not spi:
        issues.append("SelfPacedLabProvisionItem not found")

    pool_size = int(((spi or {}).get("spec") or {}).get("poolSize") or 0)
    pool_count = ((spl or {}).get("status") or {}).get("poolCount") or {}
    ready = int(pool_count.get("ready") or 0)
    provisioning = int(pool_count.get("provisioning") or 0)
    assigned = int(pool_count.get("assigned") or 0)
    spi_status = (spi or {}).get("status") or {}
    if spi_status:
        ready = int(spi_status.get("readyCount") or ready)
        provisioning = int(spi_status.get("provisioningCount") or provisioning)
        assigned = int(spi_status.get("assignedCount") or assigned)

    _, action = desired_pool_action(schedule)
    if action == "flush" or pool_size == 0:
        healthy = True
        ready_ok = True
    else:
        warm = ready + provisioning
        ready_ok = warm >= pool_size or ready >= max(1, pool_size // 2)
        if not ready_ok:
            issues.append(f"pool ready+provisioning={warm} < poolSize={pool_size}")
        healthy = not issues

    status = (
        "✅ SELFPACEDLAB HEALTHY"
        if healthy and (spl and spi)
        else ("⚠️ SELFPACEDLAB WARMING" if spl and spi and not ready_ok else "❌ SELFPACEDLAB UNHEALTHY")
    )
    return {
        "ci_name": schedule.ci_name,
        "ci": schedule.ci,
        "namespace": ns,
        "scheduled": "Yes",
        "deployed": "Yes" if spl else "No",
        "status": status,
        "expected_seats": pool_size,
        "actual_seats": ready,
        "workshop_users_assigned": f"{assigned}/{pool_size}" if pool_size else f"{assigned}/0",
        "total_seats": pool_size,
        "seats_match": "Yes" if healthy else "No",
        "healthy": healthy,
        "ready": ready_ok if (spl and spi) else False,
        "provisioned": ready_ok if (spl and spi) else False,
        "lock_status": None,
        "provisioning_date": schedule.provisioning_date,
        "auto_stop": schedule.auto_stop,
        "auto_destroy": schedule.auto_destroy,
        "actual_start": "",
        "actual_destroy": "",
        "issues": "; ".join(issues),
        "resourceclaim_name": name,
        "link_to_service": f"/selfpacedlabs/{ns}/{name}",
        "landing_page_url": "",
        "item_type": "selfpacedlab",
        "skip_soundcheck": True,
        "pool_ready": ready,
        "pool_provisioning": provisioning,
        "pool_assigned": assigned,
    }
