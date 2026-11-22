"""SelfPacedLab create / upsert / scale / flush for Flow deploys.

Week-long SelfPacedLab + SelfPacedLabProvisionItem; poolSize from CSV Instances.
Provisioning window → scale up; Auto-stop → flush (poolSize=0 + delete unclaimed RCs);
Auto-destroy → SelfPacedLab lifespan.end (week end).
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from rhdp_flow import RHDPConfig, WorkshopSchedule

logger = logging.getLogger(__name__)

BABYLON_GROUP = "babylon.gpte.redhat.com"
BABYLON_VERSION = "v1"
SPL_PLURAL = "selfpacedlabs"
SPI_PLURAL = "selfpacedlabprovisionitems"
POOLBOY_GROUP = "poolboy.gpte.redhat.com"

LABAGATOR_EVENT_ID_LABEL = "labagator.redhat.com/event-id"
LABAGATOR_LAB_CODE_LABEL = "labagator.redhat.com/lab-code"
LABAGATOR_SESSION_DATE_LABEL = "labagator.redhat.com/session-date"
CATALOG_ITEM_NAME_LABEL = f"{BABYLON_GROUP}/catalogItemName"
CATALOG_ITEM_NS_LABEL = f"{BABYLON_GROUP}/catalogItemNamespace"
SELFPACEDLAB_LABEL = f"{BABYLON_GROUP}/selfpacedlab"

DEFAULT_ASSIGNED_LIFESPAN = "4h"
DEFAULT_UNASSIGNED_LIFESPAN = "24h"


def is_selfpaced_schedule(schedule) -> bool:
    """True when CSV Item_Type (or deploy mode) selects SelfPacedLab path."""
    raw = (getattr(schedule, "item_type", None) or "").strip().lower().replace("_", "").replace("-", "")
    if raw in {"selfpacedlab", "selfpaced", "spl"}:
        return True
    return False


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


def _format_iso(dt: datetime | None) -> str:
    if not dt:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def desired_pool_action(schedule, *, now: datetime | None = None) -> tuple[Optional[int], str]:
    """Return (poolSize or None, action) for this CSV row vs wall clock.

    - scale_up → set poolSize to Instances
    - flush → poolSize 0 + delete unclaimed claims
    - defer → upsert CR only; do not change poolSize (future day's row)
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


def catalog_namespace_for(schedule, config) -> str:
    from rhdp_flow import get_catalog_namespace

    return get_catalog_namespace(schedule.ci, getattr(schedule, "catalog_namespace", None) or None)


def build_selfpacedlab_dict(schedule, config, *, name: str, lifespan_start: str, lifespan_end: str) -> dict:
    """Build SelfPacedLab CR body."""
    ci = schedule.ci
    cat_ns = catalog_namespace_for(schedule, config)
    labels = {
        CATALOG_ITEM_NAME_LABEL: ci,
        CATALOG_ITEM_NS_LABEL: cat_ns,
        f"demo.redhat.com/white-glove": str(bool(getattr(schedule, "white_glove", True))).lower(),
    }
    event_id = (getattr(schedule, "event_id", None) or "").strip()
    if event_id:
        labels[LABAGATOR_EVENT_ID_LABEL] = event_id
    lab_code = (getattr(schedule, "lab_code", None) or "").strip()
    if lab_code:
        labels[LABAGATOR_LAB_CODE_LABEL] = lab_code
    session_date = (getattr(schedule, "session_date", None) or "").strip()[:10]
    if session_date:
        labels[LABAGATOR_SESSION_DATE_LABEL] = session_date

    display = (getattr(schedule, "workshop_name", None) or getattr(schedule, "ci_name", None) or name).strip()
    spec: dict = {
        "openRegistration": True,
        "lifespan": {},
    }
    if lifespan_start:
        spec["lifespan"]["start"] = lifespan_start
    if lifespan_end:
        spec["lifespan"]["end"] = lifespan_end
    if display:
        spec["displayName"] = display
    password = (getattr(schedule, "password", None) or "").strip()
    if password:
        spec["accessPassword"] = password

    annotations = {
        f"{BABYLON_GROUP}/category": "SelfPaced",
        f"{BABYLON_GROUP}/purpose": getattr(schedule, "purpose", None) or "QA",
    }
    if getattr(schedule, "event_name", None):
        annotations["labagator.redhat.com/event-name"] = str(schedule.event_name).strip()

    return {
        "apiVersion": f"{BABYLON_GROUP}/{BABYLON_VERSION}",
        "kind": "SelfPacedLab",
        "metadata": {
            "name": name,
            "namespace": schedule.namespace,
            "labels": labels,
            "annotations": annotations,
        },
        "spec": spec,
    }


def build_provision_item_dict(
    schedule,
    config,
    *,
    name: str,
    spl_uid: str,
    pool_size: int,
    lifespan_end_hint: str = "",
) -> dict:
    """Build SelfPacedLabProvisionItem CR body."""
    _ = lifespan_end_hint
    ci = schedule.ci
    cat_ns = catalog_namespace_for(schedule, config)
    concurrency = getattr(schedule, "concurrency", None) or max(pool_size, 1)
    labels = {
        CATALOG_ITEM_NAME_LABEL: ci,
        CATALOG_ITEM_NS_LABEL: cat_ns,
        SELFPACEDLAB_LABEL: name,
    }
    event_id = (getattr(schedule, "event_id", None) or "").strip()
    if event_id:
        labels[LABAGATOR_EVENT_ID_LABEL] = event_id
    lab_code = (getattr(schedule, "lab_code", None) or "").strip()
    if lab_code:
        labels[LABAGATOR_LAB_CODE_LABEL] = lab_code

    parameters: dict = {}
    purpose = getattr(schedule, "purpose", None)
    if purpose:
        parameters["purpose"] = purpose
    activity = getattr(schedule, "activity", None)
    if activity:
        parameters["purpose_activity"] = activity

    return {
        "apiVersion": f"{BABYLON_GROUP}/{BABYLON_VERSION}",
        "kind": "SelfPacedLabProvisionItem",
        "metadata": {
            "name": name,
            "namespace": schedule.namespace,
            "labels": labels,
            "ownerReferences": [
                {
                    "apiVersion": f"{BABYLON_GROUP}/{BABYLON_VERSION}",
                    "controller": True,
                    "kind": "SelfPacedLab",
                    "name": name,
                    "uid": spl_uid,
                }
            ],
        },
        "spec": {
            "catalogItem": {"name": ci, "namespace": cat_ns},
            "poolSize": int(pool_size),
            "assignedLifespan": DEFAULT_ASSIGNED_LIFESPAN,
            "unassignedLifespan": DEFAULT_UNASSIGNED_LIFESPAN,
            "concurrency": int(concurrency) if int(concurrency) >= 1 else 1,
            "startDelay": 10,
            "selfPacedLabName": name,
            "parameters": parameters,
        },
    }


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
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=getattr(config, "timeout", 60), env=_oc_env(config))
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def _oc_apply(config, body: dict, namespace: str) -> tuple[bool, str]:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
        json.dump(body, tmp, indent=2)
        path = tmp.name
    try:
        cmd = [config.oc_command, "apply", "-f", path, "-n", namespace]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=getattr(config, "timeout", 60), env=_oc_env(config))
        if result.returncode != 0:
            return False, (result.stderr or result.stdout or "oc apply failed")[:400]
        return True, result.stdout.strip()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _oc_patch_merge(config, plural: str, name: str, namespace: str, patch: dict) -> tuple[bool, str]:
    cmd = [
        config.oc_command,
        "patch",
        f"{plural}.{BABYLON_GROUP}",
        name,
        "-n",
        namespace,
        "--type",
        "merge",
        "-p",
        json.dumps(patch),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=getattr(config, "timeout", 60), env=_oc_env(config))
    if result.returncode != 0:
        return False, (result.stderr or result.stdout or "oc patch failed")[:400]
    return True, result.stdout.strip()


def patch_pool_size(config, name: str, namespace: str, pool_size: int) -> tuple[bool, str]:
    """Patch SelfPacedLabProvisionItem.spec.poolSize."""
    return _oc_patch_merge(
        config,
        SPI_PLURAL,
        name,
        namespace,
        {"spec": {"poolSize": int(pool_size)}},
    )


def flush_unclaimed_resourceclaims(config, spl_name: str, namespace: str) -> int:
    """Delete ResourceClaims labeled for this SelfPacedLab that are not assigned.

    Returns number of delete attempts (best-effort).
    """
    cmd = [
        config.oc_command,
        "get",
        f"resourceclaims.{POOLBOY_GROUP}",
        "-n",
        namespace,
        "-l",
        f"{SELFPACEDLAB_LABEL}={spl_name}",
        "-o",
        "json",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=getattr(config, "timeout", 60), env=_oc_env(config))
    if result.returncode != 0:
        logger.warning("list ResourceClaims for flush failed: %s", result.stderr)
        return 0
    try:
        items = json.loads(result.stdout).get("items") or []
    except json.JSONDecodeError:
        return 0

    deleted = 0
    for rc in items:
        meta = rc.get("metadata") or {}
        name = meta.get("name")
        if not name:
            continue
        # Assigned claims carry user assignment annotations/status; skip if assigned.
        status = rc.get("status") or {}
        if status.get("userAssignment") or (rc.get("spec") or {}).get("userAssignment"):
            continue
        labels = meta.get("labels") or {}
        if labels.get(f"{BABYLON_GROUP}/user-assignment"):
            continue
        del_cmd = [
            config.oc_command,
            "delete",
            f"resourceclaims.{POOLBOY_GROUP}",
            name,
            "-n",
            namespace,
            "--wait=false",
        ]
        dr = subprocess.run(del_cmd, capture_output=True, text=True, timeout=getattr(config, "timeout", 60), env=_oc_env(config))
        if dr.returncode == 0:
            deleted += 1
            logger.info("Flushed unclaimed ResourceClaim %s/%s", namespace, name)
        else:
            logger.warning("Failed to delete ResourceClaim %s: %s", name, dr.stderr)
    return deleted


def _merge_lifespan(existing: dict | None, start_iso: str, end_iso: str) -> tuple[str, str]:
    """Widen lifespan to earliest start and latest end."""
    spec = (existing or {}).get("spec") or {}
    life = spec.get("lifespan") or {}
    cur_start = life.get("start") or ""
    cur_end = life.get("end") or ""

    def _earlier(a: str, b: str) -> str:
        if not a:
            return b
        if not b:
            return a
        return a if a <= b else b

    def _later(a: str, b: str) -> str:
        if not a:
            return b
        if not b:
            return a
        return a if a >= b else b

    return _earlier(cur_start, start_iso), _later(cur_end, end_iso)


def upsert_selfpacedlab_and_provision(
    schedule: "WorkshopSchedule",
    config: "RHDPConfig",
) -> tuple[str | None, str]:
    """Create or update SelfPacedLab + ProvisionItem; scale or flush poolSize.

    Returns (resource_name, status_message).
    """
    name = stable_selfpacedlab_name(schedule)
    ns = schedule.namespace
    prov_dt = _parse_dt(schedule.provisioning_date)
    stop_dt = _parse_dt(schedule.auto_stop)
    destroy_dt = _parse_dt(schedule.auto_destroy)

    # Week lifespan: start from this row's provision (or existing); end from auto_destroy if set.
    start_iso = _format_iso(prov_dt) if prov_dt else ""
    end_iso = _format_iso(destroy_dt) if destroy_dt else (_format_iso(stop_dt) if stop_dt else "")

    pool_size, action = desired_pool_action(schedule)
    logger.info(
        "SelfPacedLab %s/%s action=%s poolSize=%s",
        ns,
        name,
        action,
        pool_size,
    )

    if config.dry_run:
        logger.info(
            "[DRY-RUN] Would upsert SelfPacedLab %s (lifespan %s→%s, action=%s, poolSize=%s)",
            name,
            start_iso,
            end_iso,
            action,
            pool_size,
        )
        return name, f"dry-run:{action}"

    existing = _oc_get_json(config, SPL_PLURAL, name, ns)
    start_iso, end_iso = _merge_lifespan(existing, start_iso, end_iso)

    spl_body = build_selfpacedlab_dict(
        schedule,
        config,
        name=name,
        lifespan_start=start_iso,
        lifespan_end=end_iso,
    )
    ok, msg = _oc_apply(config, spl_body, ns)
    if not ok:
        logger.error("SelfPacedLab apply failed: %s", msg)
        return None, msg

    # Re-get for uid
    spl = _oc_get_json(config, SPL_PLURAL, name, ns)
    if not spl:
        return None, "SelfPacedLab apply succeeded but get failed"
    spl_uid = (spl.get("metadata") or {}).get("uid") or ""

    # Initial pool size for create: use target if scaling, else 0
    initial_pool = 0 if pool_size is None else int(pool_size)
    existing_spi = _oc_get_json(config, SPI_PLURAL, name, ns)
    if not existing_spi:
        spi_body = build_provision_item_dict(
            schedule,
            config,
            name=name,
            spl_uid=spl_uid,
            pool_size=initial_pool,
        )
        ok, msg = _oc_apply(config, spi_body, ns)
        if not ok:
            logger.error("SelfPacedLabProvisionItem create failed: %s", msg)
            return None, msg
    elif action != "defer" and pool_size is not None:
        ok, msg = patch_pool_size(config, name, ns, int(pool_size))
        if not ok:
            logger.error("poolSize patch failed: %s", msg)
            return None, msg

    # Always refresh lifespan on existing SPL (already applied above).
    if action == "flush":
        flush_unclaimed_resourceclaims(config, name, ns)
        if pool_size is not None:
            patch_pool_size(config, name, ns, 0)

    # Optional: extend lifespan.end when this row carries week destroy
    if destroy_dt and end_iso:
        _oc_patch_merge(
            config,
            SPL_PLURAL,
            name,
            ns,
            {"spec": {"lifespan": {"start": start_iso, "end": end_iso}}},
        )

    return name, action


def scale_selfpaced_labs(schedules, config, target_count: int) -> None:
    """Patch poolSize on SelfPacedLabProvisionItems for selfpaced CSV rows."""
    for schedule in schedules:
        if not is_selfpaced_schedule(schedule):
            continue
        name = stable_selfpacedlab_name(schedule)
        logger.info("Scaling SelfPacedLabProvisionItem %s to poolSize=%s", name, target_count)
        if config.dry_run:
            continue
        ok, msg = patch_pool_size(config, name, schedule.namespace, int(target_count))
        if not ok:
            logger.error("scale failed for %s: %s", name, msg)
        elif int(target_count) == 0:
            flush_unclaimed_resourceclaims(config, name, schedule.namespace)


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
            # After flush expect 0; mid-scale may still be draining.
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
        "actual_seats": actual_pool if actual_pool is not None else "",
        "workshop_users_assigned": "",
        "total_seats": actual_pool if actual_pool is not None else "",
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
    # Prefer provision item status when present
    spi_status = (spi or {}).get("status") or {}
    if spi_status:
        ready = int(spi_status.get("readyCount") or ready)
        provisioning = int(spi_status.get("provisioningCount") or provisioning)
        assigned = int(spi_status.get("assignedCount") or assigned)

    _, action = desired_pool_action(schedule)
    if action == "flush" or pool_size == 0:
        # Flushed overnight — ready≈0 is OK
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
