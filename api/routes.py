"""API endpoints — thin wrappers around rhdp_flow functions."""

import asyncio
import csv
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, StreamingResponse
from sse_starlette.sse import EventSourceResponse

from api.auth import verify_api_key

# Ensure parent directory is on sys.path so we can import rhdp_flow
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api import cluster_targets, identity, jobs
from api.limiter import limiter as _route_limiter
from api.log_capture import get_log_dir, start_log_capture, stop_log_capture
from api.models import (
    CatalogItemEntry,
    CatalogItemParameter,
    CatalogNamespaceMismatch,
    CatalogNamespaceValidationResponse,
    ClusterNeedsResponse,
    ClusterTenantValidationError,
    ClusterTenantValidationResponse,
    ClusterTenantValidationWarning,
    CreateTenantClusterPoolsRequest,
    DeleteResultsRequest,
    DeploymentResultResponse,
    DeployRequest,
    DestroyCheckRequest,
    DestroyCheckResponse,
    DestroyCheckResult,
    DiffEntry,
    DiffResponse,
    DisableAutostopRequest,
    ExtendRequest,
    FillMissingDatesRequest,
    HealthResponse,
    JobResponse,
    JobStatus,
    LabagatorEventsResponse,
    LabagatorEventSummary,
    LabagatorImportRequest,
    LabagatorPreviewResponse,
    LockRequest,
    NumUsersValidationResponse,
    NumUsersViolation,
    OperationResponse,
    PoolCapacityValidationResponse,
    PoolCapacityWarning,
    PoolInfo,
    PoolLookupResponse,
    PoolNotFoundWarning,
    QARequest,
    QAResultItem,
    RetryRequest,
    ScaleRequest,
    SessionSummary,
    ShowroomAppSetRequest,
    ShowroomCleanupRequest,
    ShowroomHealthRequest,
    ShowroomPreflightRequest,
    UploadResponse,
    UsersNotInCatalogAdvisory,
    WorkshopScheduleResponse,
)
from api.services import labagator_client
from api.services.labagator_import import transform_labagator_to_flow
from rhdp_flow import (
    DeploymentResult,
    RHDPConfig,
    WorkshopSchedule,
    _dedup_qa_results,
    _merge_qa1_qa2,
    analyze_cluster_tenant_relationships,
    check_showroom_health,
    create_multi_workshop_from_group,
    derive_base_domain,
    disable_autostop,
    export_student_landing_page_csv,
    extend_destroy_time,
    extend_stop_time,
    find_similar_catalog_items,
    generate_showroom_applicationset,
    get_catalog_item_num_users_limit,
    get_catalog_namespace,
    import_namespace_to_csv,
    list_catalog_items,
    load_asset_passwords,
    lock_workshops,
    process_schedule,
    qa1_verify_setup,
    qa2_verify_deployment_status,
    qa3_verify_catalog_items_exist,
    qa_destroy_check,
    read_csv_input,
    run_demolition_preflight,
    scale_workshops,
    teardown_showroom,
    unlock_workshops,
    update_passwords,
    users_column_ignored_by_catalog_advisory,
    utc_timestamp_str,
    validate_catalog_item_exists,
    validate_cluster_before_tenant,
)

logger = logging.getLogger("rhdp_flow.api")

router = APIRouter()

# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------
_schedules: list[WorkshopSchedule] = []
_qa_results: list[QAResultItem] = []
_csv_filepath: str | None = None  # stashed for QA functions that need a path
_current_filename: str = ""
_asset_passwords: dict[str, str] | None = None
_deploy_log_path: str | None = None
_qa_log_path: str | None = None
_destroy_check_results: list[dict] = []

# ---------------------------------------------------------------------------
# Result persistence — survives server restarts
# ---------------------------------------------------------------------------

_RESULTS_PERSIST_FILE = Path(
    os.environ.get(
        "RHDP_RESULTS_FILE",
        str(Path.home() / ".rhdp-flow" / "last_results.json"),
    )
)


def _save_results(results: list[DeploymentResult]) -> None:
    try:
        _RESULTS_PERSIST_FILE.parent.mkdir(parents=True, exist_ok=True)
        _RESULTS_PERSIST_FILE.write_text(
            json.dumps([asdict(r) for r in results], indent=2)
        )
    except OSError as exc:
        logger.warning("Could not persist results: %s", exc)


def _load_results() -> list[DeploymentResult]:
    try:
        if _RESULTS_PERSIST_FILE.exists():
            data = json.loads(_RESULTS_PERSIST_FILE.read_text())
            return [DeploymentResult(**r) for r in data]
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.warning("Could not load persisted results: %s", exc)
    return []


_deployment_results: list[DeploymentResult] = _load_results()
if _deployment_results:
    logger.info("Restored %d deployment result(s) from previous session", len(_deployment_results))

# Session history — each completed upload+deploy cycle gets archived here
_sessions: list[dict] = []
_session_counter: int = 0
MAX_SESSIONS = 50
MAX_UPLOAD_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB


def _validate_export_yaml_dir(path: str) -> str:
    """Validate export_yaml_dir to prevent arbitrary filesystem writes.

    Returns resolved safe path or raises HTTPException if invalid.
    """
    import tempfile
    from pathlib import Path

    # Allow only paths under temp directory or a designated 'exports' subdirectory
    allowed_prefixes = [
        Path(tempfile.gettempdir()).resolve(),
        Path.cwd() / "exports",
        Path.cwd() / "tmp",
    ]

    try:
        resolved_path = Path(path).expanduser().resolve()
    except (ValueError, OSError) as e:
        raise HTTPException(status_code=400, detail=f"Invalid path: {e}")

    # Check if path is under any allowed prefix
    for allowed in allowed_prefixes:
        try:
            resolved_path.relative_to(allowed)
            return str(resolved_path)
        except ValueError:
            continue

    raise HTTPException(
        status_code=400,
        detail=f"Export path must be under temp directory or exports/tmp subdirectory. Got: {resolved_path}"
    )


# Built-in schedule examples (files under docs/examples/)
_SCHEDULE_EXAMPLES: dict[str, tuple[str, str]] = {
    "basic": ("basic_workshop.csv", "Basic workshop"),
    "full": ("full_featured.csv", "Full featured"),
    "minimal": ("minimal_workshop.csv", "Minimal"),
}


def _schedule_examples_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "docs" / "examples"


def _write_qa_csv_for_namespace(namespace: str) -> str:
    """Write a temporary CSV containing only schedules for one namespace."""
    filtered = [s for s in _schedules if s.namespace == namespace]
    if not filtered:
        raise HTTPException(400, f'No loaded schedules match namespace "{namespace}".')

    fieldnames = [
        "CI Name",
        "CI",
        "Namespace",
        "Users",
        "Enable_workshop_interface",
        "Password",
        "Activity",
        "Purpose",
        "Workshop Name",
        "Provisioning Date (UTC)",
        "Auto-stop (UTC)",
        "Auto-destroy (UTC)",
        "Multi_Asset",
        "Asset_CIs",
        "Multi_Workshop_Name",
        "Concurrency",
        "Instances",
        "Salesforce IDs",
        "Salesforce_Type",
        "Count",
        "AWS_Region",
        "Redirect",
        "Catalog_Namespace",
        "Showroom_Repo",
        "Showroom_Ref",
        "Showroom_NoVNC",
        "Showroom_Zerotouch",
        "White_Glove",
        "Item_Type",
        "Cluster_CI",
    ]
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".csv", mode="w", newline="", encoding="utf-8")
    with tmp:
        writer = csv.DictWriter(tmp, fieldnames=fieldnames)
        writer.writeheader()
        for s in filtered:
            writer.writerow({
                "CI Name": s.ci_name,
                "CI": s.ci,
                "Namespace": s.namespace,
                "Users": "" if s.users is None else s.users,
                "Enable_workshop_interface": s.enable_workshop_interface,
                "Password": s.password,
                "Activity": s.activity,
                "Purpose": s.purpose,
                "Workshop Name": s.workshop_name,
                "Provisioning Date (UTC)": s.provisioning_date,
                "Auto-stop (UTC)": s.auto_stop,
                "Auto-destroy (UTC)": s.auto_destroy,
                "Multi_Asset": s.is_multi_asset,
                "Asset_CIs": s.asset_cis,
                "Multi_Workshop_Name": s.multi_workshop_name,
                "Concurrency": "" if s.concurrency is None else s.concurrency,
                "Instances": "" if s.instances is None else s.instances,
                "Salesforce IDs": s.salesforce_ids,
                "Salesforce_Type": s.salesforce_type,
                "Count": "" if s.count is None else s.count,
                "AWS_Region": s.aws_regions,
                "Redirect": s.redirect,
                "Catalog_Namespace": s.catalog_namespace,
                "Showroom_Repo": s.showroom_repo,
                "Showroom_Ref": s.showroom_ref,
                "Showroom_NoVNC": s.showroom_novnc,
                "Showroom_Zerotouch": s.showroom_zerotouch,
                "White_Glove": s.white_glove,
                "Item_Type": s.item_type if s.item_type else "",
                "Cluster_CI": s.cluster_ci_override if s.cluster_ci_override else "",
            })
    return tmp.name


def _coerce_optional_int(val) -> int | None:
    if val is None or val == "":
        return None
    if isinstance(val, bool):
        return int(val)
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return int(val)
    try:
        return int(str(val).strip())
    except ValueError:
        return None


_EMOJI_RE = re.compile(r"[✅❌⚠️🔴🟢🟡]+\s*")


def _clean_status(raw: str) -> str:
    """Strip emoji and normalize status string for consistent frontend display."""
    if not raw:
        return raw
    cleaned = _EMOJI_RE.sub("", raw).strip()
    return cleaned


def _normalize_qa_result_dict(r: dict) -> dict:
    """Align QA1 / QA2 dict keys so QAResultItem and the UI see consistent fields."""
    out = dict(r)

    # --- status: strip emoji for clean UI display ---
    if out.get("status"):
        out["status"] = _clean_status(str(out["status"]))

    # --- expected_users: QA1 uses expected_users, QA2 uses expected_seats ---
    eu = out.get("expected_users")
    if eu in (None, ""):
        es = out.get("expected_seats")
        out["expected_users"] = _coerce_optional_int(es) if es not in (None, "") else None
    elif not isinstance(eu, int):
        out["expected_users"] = _coerce_optional_int(eu)

    # --- actual_count: QA1 uses actual_users or actual_count, QA2 uses actual_seats ---
    ac = out.get("actual_count")
    if ac is None or ac == "":
        found = False
        for key in ("actual_seats", "actual_users"):
            raw = out.get(key)
            if raw is not None and raw != "":
                out["actual_count"] = _coerce_optional_int(raw)
                found = True
                break
        if not found:
            out["actual_count"] = None

    # --- deployed: derive from provisioned if missing ---
    if not out.get("deployed") and out.get("provisioned") is not None:
        out["deployed"] = "Yes" if out.get("provisioned") else "No"

    return out




# Cached base domain derived from the connected cluster
_cached_base_domain: str | None = None

# Thread-safe lock for global state mutations (sync endpoints run in threadpool)
_state_lock = threading.Lock()


def _rate_limit(limit_string: str):
    """Apply per-route rate limit if slowapi is available, otherwise no-op."""
    if _route_limiter:
        return _route_limiter.limit(limit_string)
    return lambda f: f


_NAMESPACE_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


def _validate_namespace(ns: str) -> str:
    """Validate a Kubernetes namespace name. Raises HTTPException on invalid input."""
    if not ns or len(ns) > 63 or not _NAMESPACE_RE.match(ns):
        raise HTTPException(400, "Invalid namespace: must match [a-z0-9-], 1-63 chars")
    return ns


def _detect_and_cache_base_domain() -> str:
    """Run `oc whoami --show-server`, derive base domain, and cache it."""
    global _cached_base_domain
    with _state_lock:
        if _cached_base_domain is not None:
            return _cached_base_domain
    try:
        env = os.environ.copy()
        kc = os.environ.get("KUBECONFIG")
        if kc:
            env["KUBECONFIG"] = kc
        r = subprocess.run(
            ["oc", "whoami", "--show-server"],
            capture_output=True, text=True, timeout=10, env=env,
        )
        with _state_lock:
            if r.returncode == 0:
                _cached_base_domain = derive_base_domain(r.stdout.strip())
            else:
                _cached_base_domain = "integration.demo.redhat.com"
    except Exception as exc:
        logger.warning("Base domain detection failed, using fallback: %s", exc)
        with _state_lock:
            _cached_base_domain = "integration.demo.redhat.com"
    return _cached_base_domain


def _get_config(
    dry_run: bool = False,
    resource_lock: bool = True,
    enable_resource_pools: bool = False,
    white_glove: bool = True,
    redirect: bool = True,
    target_cluster: str | None = None,
) -> RHDPConfig:
    config = RHDPConfig()
    config.dry_run = dry_run
    # When a deploy-target cluster is chosen, build an ephemeral kubeconfig for
    # it; otherwise fall back to KUBECONFIG / the in-cluster ServiceAccount.
    # Callers that pass target_cluster must clean up config.kubeconfig_path via
    # cluster_targets.cleanup_kubeconfig when finished.
    if target_cluster:
        config.kubeconfig_path = cluster_targets.resolve_kubeconfig(target_cluster)
    else:
        config.kubeconfig_path = os.environ.get("KUBECONFIG")
    config.resource_lock = resource_lock
    config.enable_resource_pools = enable_resource_pools
    config.white_glove = white_glove
    config.redirect = redirect
    if target_cluster:
        # Never use the hosting cluster's cached domain for another target.
        with open(config.kubeconfig_path) as stream:
            server = json.load(stream)["clusters"][0]["cluster"]["server"]
        config.base_domain = derive_base_domain(server)
    else:
        config.base_domain = _detect_and_cache_base_domain()
    config.agnosticv_repo_url = os.environ.get("AGNOSTICV_REPO_URL", config.agnosticv_repo_url)
    config.agnosticv_cache_dir = os.environ.get("AGNOSTICV_CACHE_DIR", config.agnosticv_cache_dir)
    config.agnosticv_ssh_key_path = os.environ.get("AGNOSTICV_SSH_KEY_PATH")
    ttl = os.environ.get("AGNOSTICV_REFRESH_TTL_SECONDS")
    if ttl:
        config.agnosticv_refresh_ttl_seconds = int(ttl)
    return config


def _request_config(request: Request):
    """Resolve a request's target without changing process-wide credentials."""
    target = request.headers.get("X-RHDP-Target-Cluster") or request.query_params.get("target_cluster")
    if target:
        identity.require_picker_access(request)
    try:
        config = _get_config(target_cluster=target) if target else _get_config()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(503, "Target credential lookup timed out") from exc
    try:
        yield config
    finally:
        if target:
            cluster_targets.cleanup_kubeconfig(config.kubeconfig_path)


def _config_env(config: RHDPConfig) -> dict[str, str]:
    env = os.environ.copy()
    if config.kubeconfig_path:
        env["KUBECONFIG"] = config.kubeconfig_path
    return env


def _tenant_refs(schedules, config):
    from lib.tenant_cluster_capacity import check_tenant_cluster_references

    try:
        return check_tenant_cluster_references(schedules, env=_config_env(config))
    except Exception as exc:
        raise HTTPException(502, f"Cannot verify tenant prerequisites on the selected target: {exc}") from exc


def _tenant_validation(schedules, config, fail_closed=False):
    refs = _tenant_refs(schedules, config)
    def key(record):
        return record["ci"], record["target_namespace"]
    blocked = refs["ref_no_pool"] + refs["pool_no_capacity"]
    blocked_keys = {key(r) for r in blocked}
    managed = {key(r) for r in refs["ready"] if r["managed_by_workshop"]} - blocked_keys
    legacy = [s for s in schedules if (s.ci, s.namespace) not in managed]
    validation = validate_cluster_before_tenant(legacy, config=config)
    for record in blocked:
        message = f"{record['workshop_name']}: reference pool {record['cluster_ref']} is missing or lacks direct-claim capacity"
        validation["errors"].append(message)
        validation["error_details"].append({
            "ci_name": record["workshop_name"], "tenant_ci": record["ci"],
            "cluster_ci": record["cluster_ref"], "namespace": record["target_namespace"],
            "tenant_date": "", "cluster_date": "", "message": message,
        })
    if fail_closed and validation["errors"]:
        raise HTTPException(400, "Tenant prerequisites failed: " + "; ".join(validation["errors"]))
    return validation


def _schedule_to_response(s: WorkshopSchedule) -> WorkshopScheduleResponse:
    return WorkshopScheduleResponse(
        ci_name=s.ci_name, ci=s.ci, namespace=s.namespace, users=s.users,
        enable_workshop_interface=s.enable_workshop_interface,
        password=s.password, activity=s.activity, purpose=s.purpose,
        workshop_name=s.workshop_name, provisioning_date=s.provisioning_date,
        auto_stop=s.auto_stop, auto_destroy=s.auto_destroy,
        is_multi_asset=s.is_multi_asset, asset_cis=s.asset_cis,
        multi_workshop_name=s.multi_workshop_name,
        concurrency=s.concurrency, instances=s.instances,
        salesforce_ids=s.salesforce_ids,
        salesforce_type=s.salesforce_type,
        aws_regions=s.aws_regions,
        count=s.count,
        white_glove=s.white_glove,
        redirect=s.redirect,
        catalog_namespace=s.catalog_namespace,
        showroom_repo=s.showroom_repo,
        showroom_ref=s.showroom_ref,
        showroom_novnc=s.showroom_novnc,
        showroom_zerotouch=s.showroom_zerotouch,
        item_type=s.item_type,
        cluster_ci_override=s.cluster_ci_override,
        is_cluster=s.is_cluster,
        is_tenant=s.is_tenant,
        detected_cluster_ci=s.detected_cluster_ci,
        detection_method=s.detection_method,
        cluster_ci_source=s.cluster_ci_source,
        auto_added=s.auto_added,
    )


def _result_to_response(r: DeploymentResult) -> DeploymentResultResponse:
    return DeploymentResultResponse(**asdict(r))


def _ingest_schedule_csv_text(text: str, filename: str) -> UploadResponse:
    """Parse CSV text, replace in-memory schedules, return upload response."""
    global _schedules, _csv_filepath, _current_filename
    text = text.lstrip("\ufeff")
    if len(text.encode("utf-8")) > MAX_UPLOAD_SIZE_BYTES:
        raise HTTPException(413, "File exceeds 10 MB size limit")
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".csv", delete=False, encoding="utf-8"
    )
    tmp.write(text)
    tmp.close()
    reader = csv.reader(io.StringIO(text))
    all_rows = [row for row in reader if any(cell.strip() for cell in row)]
    total_rows = max(0, len(all_rows) - 1)
    try:
        schedules = read_csv_input(tmp.name)
    except ValueError as e:
        os.unlink(tmp.name)
        raise HTTPException(400, str(e))
    with _state_lock:
        _schedules = schedules
        _current_filename = filename
        _csv_filepath = tmp.name
    skipped = total_rows - len(schedules)
    return UploadResponse(
        count=len(schedules),
        total_rows=total_rows,
        skipped_rows=max(0, skipped),
        schedules=[_schedule_to_response(s) for s in schedules],
    )


def _filter_schedules(ci_filter: str | None) -> list[WorkshopSchedule]:
    if ci_filter:
        filtered = [s for s in _schedules if s.ci == ci_filter]
        if not filtered:
            raise HTTPException(404, f"No schedules found for CI: {ci_filter}")
        return filtered
    return list(_schedules)


def _archive_current_session():
    """Save the current state as a session if there's anything to save."""
    global _session_counter
    if not _schedules and not _deployment_results:
        return
    _session_counter += 1
    session = {
        "session_id": str(_session_counter),
        "filename": _current_filename,
        "schedules": list(_schedules),
        "deployment_results": list(_deployment_results),
        "qa_results": list(_qa_results),
        "destroy_check_results": list(_destroy_check_results),
        "csv_filepath": _csv_filepath,
        "schedule_count": len(_schedules),
        "result_count": len(_deployment_results),
        "timestamp": utc_timestamp_str(),
        "deploy_log_file": os.path.basename(_deploy_log_path) if _deploy_log_path else None,
        "qa_log_file": os.path.basename(_qa_log_path) if _qa_log_path else None,
    }
    _sessions.append(session)
    if len(_sessions) > MAX_SESSIONS:
        _sessions[:] = _sessions[-MAX_SESSIONS:]


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

@router.post("/sessions/clear")
def clear_session(_key=Depends(verify_api_key)):
    """Archive current session and reset state for a new upload."""
    global _schedules, _deployment_results, _qa_results, _csv_filepath, _current_filename, _asset_passwords, _deploy_log_path, _qa_log_path, _destroy_check_results
    with _state_lock:
        _archive_current_session()
        _schedules = []
        _deployment_results = []
        _qa_results = []
        _destroy_check_results = []
        _csv_filepath = None
        _current_filename = ""
        _asset_passwords = None
        _deploy_log_path = None
        _qa_log_path = None
        return {"message": "Session cleared", "session_count": len(_sessions)}


@router.get("/sessions", response_model=list[SessionSummary])
def list_sessions():
    """List all prior sessions."""
    return [
        SessionSummary(
            session_id=s["session_id"],
            filename=s["filename"],
            schedule_count=s["schedule_count"],
            result_count=s["result_count"],
            timestamp=s["timestamp"],
            has_results=s["result_count"] > 0,
            deploy_log_file=s.get("deploy_log_file"),
            qa_log_file=s.get("qa_log_file"),
        )
        for s in _sessions
    ]


@router.get("/sessions/{session_id}")
def get_session(session_id: str):
    """Restore a prior session's data for viewing."""
    for s in _sessions:
        if s["session_id"] == session_id:
            return {
                "session_id": s["session_id"],
                "filename": s["filename"],
                "timestamp": s["timestamp"],
                "schedules": [_schedule_to_response(sc) for sc in s["schedules"]],
                "results": [_result_to_response(r) for r in s["deployment_results"]],
                "qa_results": s["qa_results"],
                "destroy_check_results": s.get("destroy_check_results", []),
            }
    raise HTTPException(404, "Session not found")


# ---------------------------------------------------------------------------
# Schedule Management
# ---------------------------------------------------------------------------

@router.put("/schedules")
def update_schedules(schedules_data: list[WorkshopScheduleResponse], _key=Depends(verify_api_key)):
    """Update the entire schedules list (Pydantic-validated)."""
    global _schedules
    new_schedules = [
        WorkshopSchedule(**s.model_dump()) for s in schedules_data
    ]
    with _state_lock:
        _schedules = new_schedules
    return {"message": f"Updated {len(new_schedules)} schedules"}


@router.delete("/schedules/{index}")
def delete_schedule(index: int, _key=Depends(verify_api_key)):
    """Delete a schedule by its index."""
    global _schedules
    with _state_lock:
        if index < 0 or index >= len(_schedules):
            raise HTTPException(404, f"Schedule index {index} not found")
        deleted_schedule = _schedules.pop(index)
        return {"message": f"Deleted schedule: {deleted_schedule.ci_name}"}


@router.patch("/schedules/fill-missing-dates")
def fill_missing_dates(request: FillMissingDatesRequest, _key=Depends(verify_api_key)):
    """Fill missing provisioning/stop/destroy dates in schedules.

    Only updates schedules that have missing or empty date fields.
    Preserves existing valid dates.
    """
    global _schedules
    schedules_updated = 0
    fields_updated = 0

    with _state_lock:
        for schedule in _schedules:
            schedule_had_updates = False
            if not schedule.provisioning_date or not schedule.provisioning_date.strip():
                schedule.provisioning_date = request.provisioning_date
                fields_updated += 1
                schedule_had_updates = True
            if not schedule.auto_stop or not schedule.auto_stop.strip():
                schedule.auto_stop = request.auto_stop
                fields_updated += 1
                schedule_had_updates = True
            if not schedule.auto_destroy or not schedule.auto_destroy.strip():
                schedule.auto_destroy = request.auto_destroy
                fields_updated += 1
                schedule_had_updates = True
            if schedule_had_updates:
                schedules_updated += 1

    return {
        "message": f"Filled {fields_updated} field(s) in {schedules_updated} schedule(s)",
        "updated_count": fields_updated,
        "schedules_updated": schedules_updated
    }


# ---------------------------------------------------------------------------
# Debug
# ---------------------------------------------------------------------------

@router.get("/debug/config")
def debug_config(_key=Depends(verify_api_key)):
    """Show current deployment configuration for debugging."""
    config = _get_config()
    return {
        "dry_run": config.dry_run,
        "resource_lock": config.resource_lock,
        "enable_resource_pools": config.enable_resource_pools,
        "white_glove": config.white_glove,
        "redirect": config.redirect,
        "base_domain": config.base_domain,
        "kubeconfig_path": config.kubeconfig_path,
        "schedules_count": len(_schedules),
        "results_count": len(_deployment_results),
        "jobs": jobs.get_stats(),
    }


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@router.get("/healthz")
async def healthz():
    """Pod probe: API responsiveness must not depend on external clusters."""
    return {"status": "ok"}


@router.get("/health", response_model=HealthResponse)
async def health(config=Depends(_request_config)):
    env = os.environ.copy()
    if config.kubeconfig_path:
        env["KUBECONFIG"] = config.kubeconfig_path

    # 1. Check oc binary exists
    oc_installed = await asyncio.to_thread(config.validate)
    if not oc_installed:
        return HealthResponse(
            status="error",
            oc_installed=False,
            message="oc command not found or not working",
        )

    # 2. Check cluster connectivity — run blocking subprocess calls off the
    #    event loop so we don't starve other requests while waiting on oc.
    loop = asyncio.get_running_loop()

    def _run_oc(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [config.oc_command, *args],
            capture_output=True, text=True, timeout=10, env=env,
        )

    try:
        r_user, r_server = await asyncio.gather(
            loop.run_in_executor(None, _run_oc, "whoami"),
            loop.run_in_executor(None, _run_oc, "whoami", "--show-server"),
        )
        if r_user.returncode == 0 and r_server.returncode == 0:
            cluster_url = r_server.stdout.strip()
            # RHDP API probe — also non-blocking
            rhdp_ok = False
            try:
                r_cat = await loop.run_in_executor(
                    None, _run_oc,
                    "get", "--raw",
                    "/apis/babylon.gpte.redhat.com/v1/namespaces/babylon-catalog-prod/catalogitems?limit=1",
                )
                rhdp_ok = r_cat.returncode == 0
            except Exception as exc:
                logger.warning("RHDP catalog probe failed: %s", exc)
            return HealthResponse(
                status="ok",
                oc_installed=True,
                oc_connected=True,
                cluster_url=cluster_url,
                user=r_user.stdout.strip(),
                base_domain=derive_base_domain(cluster_url),
                rhdp_api_reachable=rhdp_ok,
            )
        else:
            msg_parts = []
            if r_user.stderr.strip():
                msg_parts.append(r_user.stderr.strip())
            if r_server.stderr.strip():
                msg_parts.append(r_server.stderr.strip())
            return HealthResponse(
                status="error",
                oc_installed=True,
                oc_connected=False,
                message=" | ".join(msg_parts) or "oc installed but cluster unreachable",
            )
    except Exception as e:
        return HealthResponse(
            status="error",
            oc_installed=True,
            oc_connected=False,
            message=f"Cluster connectivity check failed: {e}",
        )


# ---------------------------------------------------------------------------
# Catalog (cluster)
# ---------------------------------------------------------------------------


@router.get("/catalog/items", response_model=list[CatalogItemEntry])
@_rate_limit("30/minute")
def get_catalog_items_list(request: Request, config=Depends(_request_config)):
    """List CatalogItem resources from babylon-catalog-prod and babylon-catalog-event."""
    if not config.validate():
        raise HTTPException(503, "OpenShift client (oc) is not available on the API host")
    raw = list_catalog_items(config)
    out = []
    for x in raw:
        params = [CatalogItemParameter(**p) for p in (x.get("parameters") or [])]
        out.append(CatalogItemEntry(
            id=x["id"],
            display_name=x["display_name"],
            catalog_namespace=x["catalog_namespace"],
            description=x.get("description", ""),
            category=x.get("category", ""),
            parameters=params,
        ))
    return out


@router.get("/catalog/suggestions")
@_rate_limit("30/minute")
def get_catalog_item_suggestions(request: Request, ci: str, namespace: str = "babylon-catalog-event", limit: int = 5, config=Depends(_request_config)):
    """Find similar catalog item names (fuzzy match) for a given CI name.

    Args:
        ci: Catalog Item ID user provided (partial or incorrect)
        namespace: Catalog namespace to search (default: babylon-catalog-event)
        limit: Max number of suggestions to return (default: 5)

    Returns:
        List of suggested catalog item names
    """
    if not config.validate():
        raise HTTPException(503, "OpenShift client (oc) is not available on the API host")

    suggestions = find_similar_catalog_items(ci, namespace, config, limit=limit)
    return {"suggestions": suggestions}


# ---------------------------------------------------------------------------
# Resource Pools
# ---------------------------------------------------------------------------

@router.get("/pools/lookup")
@_rate_limit("30/minute")
def lookup_pool_for_catalog_item(request: Request, catalog_item: str, config=Depends(_request_config)):
    """
    Lookup ResourcePool for a given catalog item.

    Args:
        catalog_item: Catalog item ID to lookup pool for

    Returns:
        PoolLookupResponse with pool info if found, or null if no pool exists
    """
    from api.pool_utils import get_pool_for_catalog_item

    if not config.validate():
        raise HTTPException(503, "OpenShift client (oc) is not available on the API host")

    pool_data = get_pool_for_catalog_item(catalog_item, env=_config_env(config))

    if pool_data:
        return PoolLookupResponse(
            catalog_item=catalog_item,
            pool=PoolInfo(**pool_data),
            has_pool=True
        )
    else:
        return PoolLookupResponse(
            catalog_item=catalog_item,
            pool=None,
            has_pool=False
        )


@router.get("/pools/all")
@_rate_limit("10/minute")
def list_all_pools(request: Request, config=Depends(_request_config)):
    """
    List all ResourcePools available in the cluster.

    Returns:
        List of PoolInfo objects
    """
    from api.pool_utils import list_all_pools as get_all_pools

    if not config.validate():
        raise HTTPException(503, "OpenShift client (oc) is not available on the API host")

    pools = get_all_pools(env=_config_env(config))
    return {"pools": [PoolInfo(**p) for p in pools]}


# ---------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------

@router.get("/labagator/events", response_model=LabagatorEventsResponse)
def list_labagator_events():
    """List Labagator events happening in the next 7 days, soonest first."""
    try:
        events = labagator_client.list_events()
    except labagator_client.LabagatorError:
        return LabagatorEventsResponse(events=[], error="labagator_unreachable")

    try:
        return LabagatorEventsResponse(
            events=[
                LabagatorEventSummary(
                    id=e["id"], name=e["name"], start_date=e["start_date"],
                    end_date=e["end_date"], location=e.get("location", ""),
                )
                for e in events
            ],
        )
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning("Labagator returned malformed event data: %s", exc)
        return LabagatorEventsResponse(events=[], error="labagator_unreachable")


@router.get("/schedules/labagator-preview", response_model=LabagatorPreviewResponse)
def labagator_preview(
    event_id: int,
    namespace: str,
    event_name: str,
    enable_workshop_interface: bool = True,
    concurrency: int = 10,
    white_glove: bool = True,
    auto_stop_days: int = 7,
    auto_destroy_days: int = 14,
):
    """Fetch the Flow-format CSV for a Labagator event without ingesting it."""
    _validate_namespace(namespace)
    try:
        csv_text = labagator_client.get_deploy_handoff_csv(
            event_id=event_id,
            namespace=namespace,
            enable_workshop_interface=enable_workshop_interface,
            concurrency=concurrency,
            white_glove=white_glove,
            auto_stop_days=auto_stop_days,
            auto_destroy_days=auto_destroy_days,
        )
    except labagator_client.LabagatorError as e:
        raise HTTPException(502, str(e))

    try:
        reader = csv.reader(io.StringIO(csv_text))
        all_rows = [row for row in reader if any(cell.strip() for cell in row)]
        session_count = max(0, len(all_rows) - 1)
        return LabagatorPreviewResponse(event_name=event_name, session_count=session_count, csv_text=csv_text)
    except (TypeError, ValueError) as exc:
        logger.warning("Labagator returned malformed CSV data: %s", exc)
        raise HTTPException(502, "Labagator returned invalid data")


@router.post("/schedules/upload", response_model=UploadResponse)
@_rate_limit("10/minute")
async def upload_csv(request: Request, file: UploadFile = File(...), _key=Depends(verify_api_key)):
    content = await file.read()
    if len(content) > MAX_UPLOAD_SIZE_BYTES:
        raise HTTPException(413, "File exceeds 10 MB size limit")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(400, "File must be UTF-8 encoded CSV")
    return await asyncio.to_thread(_ingest_schedule_csv_text, text, file.filename or "unknown.csv")


@router.post("/schedules/import-from-labagator", response_model=UploadResponse)
def import_from_labagator(body: LabagatorImportRequest, _key=Depends(verify_api_key)):
    """Ingest a Flow-format CSV previously fetched from Labagator via /schedules/labagator-preview."""
    return _ingest_schedule_csv_text(body.csv_text, body.filename)


@router.post("/schedules/import-labagator", response_model=UploadResponse)
async def import_labagator_sessions(
    file: UploadFile = File(...),
    default_ci: str = "PLACEHOLDER_CATALOG_ITEM",
    default_users: int | None = None,
    default_redirect: bool = True,
    default_white_glove: bool = True,
    buffer_hours: int | None = None,
    timezone_offset_hours: int = 0,
    _key=Depends(verify_api_key)
):
    """Import Labagator session export CSV and convert to Flow schedules.

    Accepts Labagator session CSV with fields:
    - Required: session_code, title, session_date, start_time, end_time
    - Optional: room, speakers, topics, audience_level, expected_attendees, track

    Global settings (applied to all imported sessions):
    - default_ci: Catalog item ID (can be edited per session after import)
    - default_users: Number of users (None = smart estimation from metadata)
    - default_redirect: Enable redirect after login (default: True)
    - default_white_glove: Enable white glove mode (default: True)
    - buffer_hours: Hours between session end and auto-destroy (None = smart calculation)
    - timezone_offset_hours: Hours to add for timezone conversion (e.g., 4 for EDT to UTC)

    Smart defaults:
    - User count estimated from audience_level or expected_attendees if available
    - Buffer hours calculated from session length (1h for <1h sessions, 2h for 1-2h, 3h for >2h)

    Returns Flow workshop schedules ready for deployment.
    """
    # Read uploaded file
    content = await file.read()
    labagator_csv = io.StringIO(content.decode("utf-8"))

    # Transform to Flow format with enhanced settings
    try:
        flow_csv = transform_labagator_to_flow(
            labagator_csv,
            default_ci=default_ci,
            default_users=default_users,
            default_redirect=default_redirect,
            default_white_glove=default_white_glove,
            buffer_hours=buffer_hours,
            timezone_offset_hours=timezone_offset_hours,
        )
    except Exception as e:
        logger.exception("Labagator transformation failed")
        raise HTTPException(400, f"Import failed: {e}")

    # Parse as Flow schedules (reuse existing upload logic)
    return await asyncio.to_thread(_ingest_schedule_csv_text, flow_csv, file.filename or "labagator-import.csv")


@router.get("/schedules/examples")
def list_schedule_examples():
    """Short labels for built-in schedule CSVs (see docs/examples/)."""
    return [{"slug": slug, "label": label} for slug, (_, label) in _SCHEDULE_EXAMPLES.items()]


@router.post("/schedules/load-example/{slug}", response_model=UploadResponse)
@_rate_limit("10/minute")
def load_schedule_example(request: Request, slug: str, _key=Depends(verify_api_key)):
    """Load a whitelisted example CSV from docs/examples/ (same effect as upload)."""
    if slug not in _SCHEDULE_EXAMPLES:
        raise HTTPException(404, f"Unknown example: {slug}")
    filename, _label = _SCHEDULE_EXAMPLES[slug]
    path = _schedule_examples_dir() / filename
    if not path.is_file():
        logger.error("Example CSV missing: %s", path)
        raise HTTPException(500, "Example file not available")
    text = path.read_text(encoding="utf-8")
    return _ingest_schedule_csv_text(text, f"example-{slug}.csv")


@router.post("/schedules/upload-passwords")
async def upload_passwords(file: UploadFile = File(...), _key=Depends(verify_api_key)):
    """Upload a per-asset passwords CSV (columns: CI, Password)."""
    global _asset_passwords
    content = await file.read()
    if len(content) > MAX_UPLOAD_SIZE_BYTES:
        raise HTTPException(413, "File exceeds 10 MB size limit")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(400, "File must be UTF-8 encoded CSV")

    import tempfile as _tf
    tmp = _tf.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, encoding="utf-8")
    tmp.write(text)
    tmp.close()
    passwords = load_asset_passwords(tmp.name)
    os.unlink(tmp.name)
    with _state_lock:
        _asset_passwords = passwords

    return {"count": len(passwords), "message": f"Loaded {len(passwords)} asset password(s)"}


@router.get("/schedules", response_model=list[WorkshopScheduleResponse])
def get_schedules():
    return [_schedule_to_response(s) for s in _schedules]


@router.post("/schedules/validate-namespaces")
def validate_namespaces(_key=Depends(verify_api_key), config=Depends(_request_config)):
    """Check whether the namespaces referenced by loaded schedules exist on the cluster."""
    if not _schedules:
        raise HTTPException(400, "No schedules loaded.")
    env = os.environ.copy()
    if config.kubeconfig_path:
        env["KUBECONFIG"] = config.kubeconfig_path

    unique_ns = {s.namespace for s in _schedules}
    for ns in unique_ns:
        _validate_namespace(ns)
    results: dict[str, bool] = {}
    for ns in unique_ns:
        try:
            r = subprocess.run(
                [config.oc_command, "get", "namespace", ns, "-o", "name"],
                capture_output=True, text=True, timeout=10, env=env,
            )
            results[ns] = r.returncode == 0
        except Exception as exc:
            logger.warning("Namespace validation failed for %s: %s", ns, exc)
            results[ns] = False
    missing = [ns for ns, ok in results.items() if not ok]
    return {"namespaces": results, "missing": missing}


@router.post("/schedules/validate-num-users", response_model=NumUsersValidationResponse)
def validate_num_users(_key=Depends(verify_api_key), config=Depends(_request_config)):
    """Check whether any loaded schedules exceed the catalog item's num_users maximum."""
    if not _schedules:
        raise HTTPException(400, "No schedules loaded.")
    violations: list[NumUsersViolation] = []
    users_not_in_catalog: list[UsersNotInCatalogAdvisory] = []
    limits: dict[str, int] = {}
    checked = 0
    skipped = 0
    ci_cache: dict[str, dict | None] = {}
    advisory_seen: set = set()

    def _check_ci(ci: str, schedule: WorkshopSchedule):
        nonlocal checked, skipped
        requested_users = schedule.users
        if requested_users is None or requested_users <= 0:
            skipped += 1
            return
        if ci not in ci_cache:
            ci_cache[ci] = get_catalog_item_num_users_limit(ci, config)
        info = ci_cache[ci]
        if info is None:
            skipped += 1
            return
        checked += 1
        adv = users_column_ignored_by_catalog_advisory(schedule, ci, info)
        if adv:
            key = (schedule.ci_name, schedule.namespace, ci, adv["severity"], adv["message"])
            if key not in advisory_seen:
                advisory_seen.add(key)
                users_not_in_catalog.append(UsersNotInCatalogAdvisory(**adv))
        if info.get("has_num_users") and info.get("maximum") is not None:
            limits[ci] = info["maximum"]
            if requested_users > info["maximum"]:
                violations.append(NumUsersViolation(
                    ci_name=schedule.ci_name,
                    ci=ci,
                    namespace=schedule.namespace,
                    requested_users=requested_users,
                    maximum=info["maximum"],
                    minimum=info.get("minimum"),
                    default_value=info.get("default"),
                ))

    for s in _schedules:
        _check_ci(s.ci, s)
        # Also check individual asset CIs for multi-asset workshops
        if s.is_multi_asset and s.asset_cis:
            for asset_ci in (c.strip() for c in s.asset_cis.split(",") if c.strip()):
                _check_ci(asset_ci, s)

    return NumUsersValidationResponse(
        violations=violations,
        users_not_in_catalog=users_not_in_catalog,
        checked=checked,
        skipped=skipped,
        limits=limits,
    )


@router.post("/schedules/validate-catalog-namespaces", response_model=CatalogNamespaceValidationResponse)
def validate_catalog_namespaces(_key=Depends(verify_api_key), config=Depends(_request_config)):
    """Check whether catalog items exist in their expected catalog namespaces."""
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if not _schedules:
        raise HTTPException(400, "No schedules loaded.")

    # Build unique (ci, expected_ns) pairs to check
    to_check: list[tuple[str, str, WorkshopSchedule]] = []
    seen_cis: set[str] = set()
    for s in _schedules:
        cis = [s.ci]
        if s.is_multi_asset and s.asset_cis:
            cis += [c.strip() for c in s.asset_cis.split(",") if c.strip()]
        for ci in cis:
            if ci not in seen_cis:
                seen_cis.add(ci)
                expected_ns = get_catalog_namespace(ci, s.catalog_namespace or None)
                to_check.append((ci, expected_ns, s))

    # Run oc checks in parallel (one thread per unique CI)
    ci_results: dict[str, tuple] = {}
    lock = threading.Lock()

    def _run(ci: str, expected_ns: str) -> None:
        result = validate_catalog_item_exists(ci, expected_ns, config)
        with lock:
            ci_results[ci] = result

    with ThreadPoolExecutor(max_workers=min(len(to_check), 16)) as executor:
        futures = {executor.submit(_run, ci, expected_ns): ci for ci, expected_ns, _ in to_check}
        for f in as_completed(futures):
            f.result()  # raise any exception

    mismatches: list[CatalogNamespaceMismatch] = []
    not_found: list[dict] = []
    checked = 0

    for s in _schedules:
        cis = [s.ci]
        if s.is_multi_asset and s.asset_cis:
            cis += [c.strip() for c in s.asset_cis.split(",") if c.strip()]
        for ci in cis:
            expected_ns = get_catalog_namespace(ci, s.catalog_namespace or None)
            exists, found_ns, suggestion = ci_results.get(ci, (True, expected_ns, None))
            checked += 1
            if not exists and found_ns is not None:
                mismatches.append(CatalogNamespaceMismatch(
                    ci_name=s.ci_name,
                    ci=ci,
                    namespace=s.namespace,
                    expected_catalog_namespace=expected_ns,
                    found_catalog_namespace=found_ns,
                    suggestion=suggestion or f"Found in {found_ns} instead of {expected_ns}",
                ))
            elif not exists and found_ns is None:
                not_found.append({
                    "ci_name": s.ci_name,
                    "ci": ci,
                    "namespace": s.namespace,
                    "expected_catalog_namespace": expected_ns,
                    "message": suggestion or "Not found in any catalog namespace",
                })

    return CatalogNamespaceValidationResponse(
        mismatches=mismatches,
        not_found=not_found,
        checked=checked,
        skipped=0,
    )


@router.post("/schedules/validate-cluster-tenant", response_model=ClusterTenantValidationResponse)
def validate_cluster_tenant_scheduling(_key=Depends(verify_api_key), config=Depends(_request_config)):
    """Check that cluster catalog items are scheduled before tenant catalog items."""
    if not _schedules:
        raise HTTPException(400, "No schedules loaded.")

    analyze_cluster_tenant_relationships(_schedules, config=config)
    validation = _tenant_validation(_schedules, config)

    tenants_checked = sum(1 for s in _schedules if s.is_tenant)
    clusters_found = sum(1 for r in validation["relationships"] if r.get("status") in ("valid", "timing_violation", "found_on_cluster"))

    return ClusterTenantValidationResponse(
        errors=[ClusterTenantValidationError(**e) for e in validation["error_details"]],
        warnings=[ClusterTenantValidationWarning(**w) for w in validation["warning_details"]],
        tenants_checked=tenants_checked,
        clusters_found=clusters_found,
    )


@router.post("/schedules/auto-fix-cluster-tenant")
def auto_fix_cluster_tenant_timing(buffer_hours: float = 4.0, _key=Depends(verify_api_key)):
    """Auto-fix cluster/tenant timing by ensuring clusters deploy BEFORE tenants.

    Adjusts cluster provisioning dates to be X hours before tenant provisioning.
    Skips clusters that will be provided by TenantClusterPools.

    Args:
        buffer_hours: Hours to deploy cluster before tenant (default: 4.0)
    """
    global _schedules
    if not _schedules:
        raise HTTPException(400, "No schedules loaded.")

    from lib.cluster_tenant_validation import auto_fix_cluster_tenant_timing

    buffer_minutes = int(buffer_hours * 60)
    result = auto_fix_cluster_tenant_timing(_schedules, buffer_minutes=buffer_minutes)
    _schedules = result["schedules"]

    return {
        "fixed_count": result["fixed_count"],
        "skipped_count": result["skipped_count"],
        "fixed_items": result["fixed_items"],
        "skipped_items": result["skipped_items"],
        "warnings": result["warnings"],
        "message": result["message"],
    }


@router.post("/schedules/validate-pool-capacity", response_model=PoolCapacityValidationResponse)
def validate_pool_capacity(_key=Depends(verify_api_key), config=Depends(_request_config)):
    if not _schedules:
        raise HTTPException(400, "No schedules loaded.")
    refs = _tenant_refs(_schedules, config)
    return PoolCapacityValidationResponse(
        tenant_items_checked=refs["total_tenant_count"],
        pools_queried=len({r["cluster_ref"] for tier in ("ready", "pool_no_capacity") for r in refs[tier]}),
        not_found=[PoolNotFoundWarning(
            ci_name=r["workshop_name"], ci=r["ci"], namespace=r["target_namespace"],
            base_ci=r["cluster_ref"],
            message=f"Missing shared reference pool {r['cluster_ref']}. Check the catalog contract on this target.",
        ) for r in refs["ref_no_pool"]],
        warnings=[PoolCapacityWarning(
            ci_name=r["workshop_name"], ci=r["ci"], namespace=r["target_namespace"],
            pool_name=r["cluster_ref"], pool_saturation_percent=100,
            placement_capacity_percent=100, severity="critical",
            message="Direct tenant claim requires available shared cluster capacity.",
        ) for r in refs["pool_no_capacity"]],
    )


@router.post("/schedules/diff", response_model=DiffResponse)
async def diff_schedules(file: UploadFile = File(...), _key=Depends(verify_api_key)):
    """Compare a new CSV against the currently loaded schedules."""
    if not _schedules:
        raise HTTPException(400, "No schedules loaded to compare against.")

    content = await file.read()
    if len(content) > MAX_UPLOAD_SIZE_BYTES:
        raise HTTPException(413, "File exceeds 10 MB size limit")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(400, "File must be UTF-8 encoded CSV")

    import tempfile
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, encoding="utf-8")
    tmp.write(text)
    tmp.close()

    try:
        new_schedules = await asyncio.to_thread(read_csv_input, tmp.name)
    except ValueError as e:
        os.unlink(tmp.name)
        raise HTTPException(400, str(e))
    os.unlink(tmp.name)

    # Build keyed maps: (ci, namespace) -> schedule
    old_map = {(s.ci, s.namespace): s for s in _schedules}
    new_map = {(s.ci, s.namespace): s for s in new_schedules}

    added = []
    removed = []
    changed = []
    unchanged = 0

    # Find added + changed
    for key, ns in new_map.items():
        if key not in old_map:
            added.append(DiffEntry(ci_name=ns.ci_name, ci=ns.ci, namespace=ns.namespace, change="added"))
        else:
            os_item = old_map[key]
            diffs = []
            for field in ("users", "provisioning_date", "auto_stop", "auto_destroy", "password", "workshop_name", "instances", "concurrency", "count", "redirect", "white_glove", "salesforce_ids", "aws_regions"):
                old_val = getattr(os_item, field)
                new_val = getattr(ns, field)
                if old_val != new_val:
                    diffs.append(f"{field}: {old_val} → {new_val}")
            if diffs:
                changed.append(DiffEntry(
                    ci_name=ns.ci_name, ci=ns.ci, namespace=ns.namespace,
                    change="changed", details="; ".join(diffs),
                ))
            else:
                unchanged += 1

    # Find removed
    for key, os_item in old_map.items():
        if key not in new_map:
            removed.append(DiffEntry(ci_name=os_item.ci_name, ci=os_item.ci, namespace=os_item.namespace, change="removed"))

    return DiffResponse(added=added, removed=removed, changed=changed, unchanged=unchanged)


# ---------------------------------------------------------------------------
# Deploy
# ---------------------------------------------------------------------------

async def _run_deploy_over(
    schedules: list[WorkshopSchedule],
    config: RHDPConfig,
    job_id: str,
    asset_passwords: dict[str, str] | None,
) -> list[DeploymentResult]:
    """Run the deploy loop over an explicit, LOCAL ``schedules`` list.

    Operates purely on its arguments — it MUST NOT read or write the module
    globals ``_schedules`` / ``_deployment_results``. Honors pause/cancel via
    ``job_id`` and pushes progress through ``jobs.update_job``. Returns the list
    of ``DeploymentResult`` for the caller to persist however it likes.
    """
    grouped_multi: dict[str, list[WorkshopSchedule]] = {}
    regular_schedules: list[WorkshopSchedule] = []
    for s in schedules:
        if s.multi_workshop_name and s.is_multi_asset:
            grouped_multi.setdefault(s.multi_workshop_name, []).append(s)
        else:
            regular_schedules.append(s)

    results: list[DeploymentResult] = []
    total = len(grouped_multi) + len(regular_schedules)
    done = 0

    # Grouped multi-asset
    for group_name, group_scheds in grouped_multi.items():
        await jobs.wait_if_paused(job_id)
        if jobs.is_cancel_requested(job_id):
            break
        # Run sync OpenShift work off the event loop so WebSocket/polling can deliver progress.
        mw_name = await asyncio.to_thread(
            create_multi_workshop_from_group, group_scheds, config
        )
        first = group_scheds[0]
        if mw_name:
            url = f"https://{config.base_domain}/multi-workshop/{first.namespace}/{mw_name}"
            results.append(DeploymentResult(
                ci_name=group_name, ci=first.ci, namespace=first.namespace,
                guid=mw_name, url=url, status="deployed_unverified",
                provisioning_date=first.provisioning_date,
                auto_stop=first.auto_stop, auto_destroy=first.auto_destroy,
                timestamp=utc_timestamp_str(),
                password=first.password,
            ))
        else:
            results.append(DeploymentResult(
                ci_name=group_name, ci=first.ci, namespace=first.namespace,
                guid="failed", url="", status="failed",
                provisioning_date=first.provisioning_date,
                auto_stop=first.auto_stop, auto_destroy=first.auto_destroy,
                timestamp=utc_timestamp_str(),
                error_message="Failed to create grouped MultiWorkshop",
                password=first.password,
            ))
        done += 1
        pct = int(done / total * 100) if total else 100
        jobs.update_job(job_id, progress=pct, message=f"Processed group: {group_name}")

    for s in regular_schedules:
        await jobs.wait_if_paused(job_id)
        if jobs.is_cancel_requested(job_id):
            break
        result = await asyncio.to_thread(
            process_schedule, s, config, asset_passwords
        )
        results.append(result)
        done += 1
        pct = int(done / total * 100) if total else 100
        jobs.update_job(
            job_id, progress=pct,
            message=f"Deployed {result.ci_name}: {result.status}",
        )
        if not config.dry_run and len(regular_schedules) > 1:
            await asyncio.sleep(1)

    return results


@router.post("/deploy", response_model=JobResponse)
@_rate_limit("10/minute")
async def deploy(request: Request, body: DeployRequest = DeployRequest(), _key=Depends(verify_api_key)):  # type: ignore
    if not _schedules:
        raise HTTPException(400, "No schedules loaded. Upload a CSV first.")

    schedules = _filter_schedules(body.ci_filter)

    # Choosing a non-default target cluster is restricted to approved operators.
    if body.target_cluster:
        identity.require_picker_access(request)
    # Validate the deploy-target cluster early so a bad target fails fast.
    try:
        await asyncio.to_thread(cluster_targets.resolve_and_cleanup_check, body.target_cluster)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    def preflight():
        config_check = _get_config(target_cluster=body.target_cluster)
        try:
            ci_cache: dict[str, dict | None] = {}
            limit_errors: list[str] = []
            ns_cache: dict[str, tuple] = {}
            not_found_errors: list[str] = []
            for s in schedules:
                if s.users is not None and s.users > 0:
                    if s.ci not in ci_cache:
                        ci_cache[s.ci] = get_catalog_item_num_users_limit(s.ci, config_check)
                    info = ci_cache[s.ci]
                    if info and info.get("maximum") is not None and s.users > info["maximum"]:
                        limit_errors.append(
                            f"{s.ci_name} ({s.ci}): {s.users} requested, max {info['maximum']}"
                        )
                # Resolve catalog namespace: auto-redirect if item lives in a different namespace
                expected_ns = get_catalog_namespace(s.ci, s.catalog_namespace or None)
                if s.ci not in ns_cache:
                    ns_cache[s.ci] = validate_catalog_item_exists(s.ci, expected_ns, config_check)
                exists, found_ns, suggestion = ns_cache[s.ci]
                if not exists and found_ns is not None and found_ns != expected_ns:
                    # Item in a different namespace — redirect silently
                    s.catalog_namespace = found_ns
                elif not exists and found_ns is None:
                    not_found_errors.append(f"{s.ci_name} ({s.ci}): {suggestion}")
        finally:
            cluster_targets.cleanup_kubeconfig(config_check.kubeconfig_path if body.target_cluster else None)
        if limit_errors:
            raise HTTPException(
                400,
                f"num_users limit exceeded: {'; '.join(limit_errors)}"
            )
        if not_found_errors:
            raise HTTPException(
                400,
                f"Catalog items not found — cannot deploy: {'; '.join(not_found_errors)}"
            )

    if not body.dry_run:
        await asyncio.to_thread(preflight)

    job = jobs.create_job()

    async def _run():
        global _deploy_log_path
        handler, log_path = start_log_capture("deploy", job.job_id)
        config = None
        try:
            config = await asyncio.to_thread(_get_config,
                dry_run=body.dry_run,
                resource_lock=body.resource_lock,
                enable_resource_pools=body.enable_resource_pools,
                white_glove=body.white_glove,
                redirect=body.redirect,
                target_cluster=body.target_cluster,
            )
            # U3: Propagate showroom deploy settings to schedules
            for s in schedules:
                if s.showroom_repo:
                    s.showroom_novnc = body.showroom_novnc
                    s.showroom_zerotouch = body.showroom_zerotouch
            jobs.update_job(
                job.job_id,
                status=jobs.Status.running,
                message="Starting deployment",
                progress=1,
            )

            # Replicate main() deploy loop logic (shared with /deploy/session)
            # Planned unit count (grouped multi-asset workshops + regular schedules)
            # used only for the "cancelled after X of Y" message below.
            _multi_groups = {
                s.multi_workshop_name
                for s in schedules
                if s.multi_workshop_name and s.is_multi_asset
            }
            total = len(_multi_groups) + sum(
                1 for s in schedules
                if not (s.multi_workshop_name and s.is_multi_asset)
            )
            results = await _run_deploy_over(
                schedules, config, job.job_id, _asset_passwords
            )

            global _deployment_results
            with _state_lock:
                _deployment_results = results
                _deploy_log_path = log_path
            _save_results(results)

            if jobs.is_cancel_requested(job.job_id):
                jobs.update_job(
                    job.job_id,
                    status=jobs.Status.cancelled,
                    message=f"Cancelled after {len(results)} of {total} deployment(s)",
                    results=[asdict(r) for r in results],
                    log_path=log_path,
                )
            else:
                jobs.update_job(
                    job.job_id,
                    status=jobs.Status.completed,
                    progress=100,
                    message=f"Completed: {len(results)} deployment(s)",
                    results=[asdict(r) for r in results],
                    log_path=log_path,
                )
        except Exception as exc:
            with _state_lock:
                _deploy_log_path = log_path
            jobs.update_job(
                job.job_id,
                status=jobs.Status.failed,
                error=str(exc),
                message=f"Deployment failed: {exc}",
                log_path=log_path,
            )
        finally:
            if body.target_cluster and config is not None:
                cluster_targets.cleanup_kubeconfig(config.kubeconfig_path)
            stop_log_capture(handler)

    asyncio.create_task(_run())
    return JobResponse(job_id=job.job_id, status=JobStatus(job.status.value), progress=job.progress)


@router.post("/deploy/session", response_model=JobResponse)
async def deploy_session(
    request: Request,
    file: UploadFile = File(...),
    dry_run: bool = Form(False),
    resource_lock: bool = Form(True),
    enable_resource_pools: bool = Form(False),
    white_glove: bool = Form(True),
    redirect: bool = Form(True),
    target_cluster: str | None = Form(None),
    _key: None = Depends(verify_api_key),
):
    """Deploy an explicitly-uploaded CSV, deploying the LOCAL parsed list.

    Correctness is isolated: the deploy runs off the CSV parsed in THIS request,
    never off the shared ``_schedules`` global — so concurrent callers cannot
    make it deploy the wrong set. For visibility, it also mirrors those
    schedules and the resulting deployment records into Flow's global state
    (``_schedules`` / ``_deployment_results``) so the deploy shows up in the
    Flow dashboard (Upload + Deployments tabs). That mirror is display-only and
    best-effort (a concurrent human upload may overwrite the displayed set).
    Per-workshop results are also on the job (``GET /api/deploy/status/{id}``).
    """
    if target_cluster:
        identity.require_picker_access(request)
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_SIZE_BYTES:
        raise HTTPException(413, "File exceeds 10 MB size limit")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(400, "File must be UTF-8 encoded CSV")

    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".csv", delete=False, encoding="utf-8"
    )
    tmp.write(text)
    tmp.close()
    try:
        schedules = await asyncio.to_thread(read_csv_input, tmp.name)  # LOCAL list — deploy runs off this
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    finally:
        os.unlink(tmp.name)

    if not schedules:
        raise HTTPException(400, "No schedules parsed from CSV")

    # Mirror parsed schedules into Flow's global state for dashboard visibility
    # (Upload tab). Display-only: the deploy below uses the LOCAL `schedules`.
    global _schedules
    with _state_lock:
        _schedules = schedules

    try:
        config = await asyncio.to_thread(_get_config,
            dry_run=dry_run,
            resource_lock=resource_lock,
            enable_resource_pools=enable_resource_pools,
            white_glove=white_glove,
            redirect=redirect,
            target_cluster=target_cluster,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    job = jobs.create_job()

    async def _run():
        try:
            jobs.update_job(
                job.job_id,
                status=jobs.Status.running,
                message="Starting deployment",
                progress=1,
            )
            results = await _run_deploy_over(
                schedules, config, job.job_id, asset_passwords={}
            )
            # Mirror results into Flow's global state + persist, so this deploy
            # shows in the Flow dashboard (Deployments tab). ACCUMULATE rather
            # than replace: merge new results into the existing set (latest wins
            # per ci+namespace) so the dashboard keeps history across deploys
            # instead of showing only the most recent one.
            global _deployment_results
            with _state_lock:
                merged = {(r.ci, r.namespace): r for r in _deployment_results}
                for r in results:
                    merged[(r.ci, r.namespace)] = r
                _deployment_results = list(merged.values())
                _save_results(_deployment_results)
            if jobs.is_cancel_requested(job.job_id):
                jobs.update_job(
                    job.job_id,
                    status=jobs.Status.cancelled,
                    message=f"Cancelled after {len(results)} deployment(s)",
                    results=[asdict(r) for r in results],
                )
            else:
                jobs.update_job(
                    job.job_id,
                    status=jobs.Status.completed,
                    progress=100,
                    message=f"Completed: {len(results)} deployment(s)",
                    results=[asdict(r) for r in results],
                )
        except Exception as exc:
            jobs.update_job(
                job.job_id,
                status=jobs.Status.failed,
                error=str(exc),
                message=f"Deployment failed: {exc}",
            )
        finally:
            if target_cluster:
                cluster_targets.cleanup_kubeconfig(config.kubeconfig_path)

    asyncio.create_task(_run())
    return JobResponse(job_id=job.job_id, status=JobStatus(job.status.value), progress=job.progress)


@router.post("/deploy/dry-run", response_model=list[DeploymentResultResponse])
@_rate_limit("10/minute")
def deploy_dry_run(request: Request, body: DeployRequest = DeployRequest(), _key=Depends(verify_api_key)):  # type: ignore
    global _deploy_log_path
    if not _schedules:
        raise HTTPException(400, "No schedules loaded. Upload a CSV first.")

    if body.target_cluster:
        identity.require_picker_access(request)
    schedules = _filter_schedules(body.ci_filter)
    try:
        config = _get_config(
            dry_run=True,
            resource_lock=body.resource_lock,
            enable_resource_pools=body.enable_resource_pools,
            white_glove=body.white_glove,
            redirect=body.redirect,
            target_cluster=body.target_cluster,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if body.export_yaml_dir:
        config.dry_run_export_yaml_dir = _validate_export_yaml_dir(body.export_yaml_dir)
        config.dry_run_yaml_export_seq = 0
    # U3: Propagate showroom deploy settings to schedules
    for s in schedules:
        if s.showroom_repo:
            s.showroom_novnc = body.showroom_novnc
            s.showroom_zerotouch = body.showroom_zerotouch

    handler, log_path = start_log_capture("deploy-dryrun")
    try:
        # Replicate main() grouping logic for accurate preview
        grouped_multi = {}
        regular_schedules = []
        for s in schedules:
            if s.multi_workshop_name and s.is_multi_asset:
                grouped_multi.setdefault(s.multi_workshop_name, []).append(s)
            else:
                regular_schedules.append(s)

        results = []
        for group_name, group_scheds in grouped_multi.items():
            mw_name = create_multi_workshop_from_group(group_scheds, config)
            first = group_scheds[0]
            if mw_name:
                url = f"https://{config.base_domain}/multi-workshop/{first.namespace}/{mw_name}"
                results.append(DeploymentResult(
                    ci_name=group_name, ci=first.ci, namespace=first.namespace,
                    guid=mw_name, url=url, status="deployed_unverified",
                    provisioning_date=first.provisioning_date,
                    auto_stop=first.auto_stop, auto_destroy=first.auto_destroy,
                    timestamp=utc_timestamp_str(),
                    password=first.password,
                ))
            else:
                results.append(DeploymentResult(
                    ci_name=group_name, ci=first.ci, namespace=first.namespace,
                    guid="failed", url="", status="failed",
                    provisioning_date=first.provisioning_date,
                    auto_stop=first.auto_stop, auto_destroy=first.auto_destroy,
                    timestamp=utc_timestamp_str(),
                    error_message="Failed to create grouped MultiWorkshop",
                    password=first.password,
                ))

        for s in regular_schedules:
            result = process_schedule(s, config, asset_passwords=_asset_passwords)
            results.append(result)

        global _deployment_results
        with _state_lock:
            _deployment_results = results
            _deploy_log_path = log_path
        _save_results(results)
        return [_result_to_response(r) for r in results]
    finally:
        if body.target_cluster:
            cluster_targets.cleanup_kubeconfig(config.kubeconfig_path)
        stop_log_capture(handler)


@router.post("/deploy/dry-run-yaml")
@_rate_limit("10/minute")
def deploy_dry_run_yaml(request: Request, body: DeployRequest = DeployRequest(), _key=Depends(verify_api_key)):  # type: ignore
    """Run the same dry-run deploy path and return concatenated manifest YAML (download).

    Writes ResourceClaim / Workshop / WorkshopProvision YAMLs to a temp directory during
    dry-run, then returns them as one file separated by ``---``. Requires schedules loaded.
    """
    if not _schedules:
        raise HTTPException(400, "No schedules loaded. Upload a CSV first.")

    if body.target_cluster:
        identity.require_picker_access(request)
    schedules = _filter_schedules(body.ci_filter)
    tmpdir = tempfile.mkdtemp(prefix="rhdp-dryrun-yaml-")
    config = None
    try:
        try:
            config = _get_config(
                dry_run=True,
                resource_lock=body.resource_lock,
                enable_resource_pools=body.enable_resource_pools,
                white_glove=body.white_glove,
                redirect=body.redirect,
                target_cluster=body.target_cluster,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        config.dry_run_export_yaml_dir = tmpdir
        config.dry_run_yaml_export_seq = 0
        for s in schedules:
            if s.showroom_repo:
                s.showroom_novnc = body.showroom_novnc
                s.showroom_zerotouch = body.showroom_zerotouch

        grouped_multi: dict[str, list[WorkshopSchedule]] = {}
        regular_schedules: list[WorkshopSchedule] = []
        for s in schedules:
            if s.multi_workshop_name and s.is_multi_asset:
                grouped_multi.setdefault(s.multi_workshop_name, []).append(s)
            else:
                regular_schedules.append(s)

        for _group_name, group_scheds in grouped_multi.items():
            create_multi_workshop_from_group(group_scheds, config)

        for s in regular_schedules:
            process_schedule(s, config, asset_passwords=_asset_passwords)

        yaml_paths = sorted(Path(tmpdir).glob("*.yaml"))
        if not yaml_paths:
            raise HTTPException(
                400,
                "No manifest YAML was generated. YAML export applies to dry-run paths that "
                "emit ResourceClaim, Workshop, or WorkshopProvision (e.g. standard single-workshop flows).",
            )
        parts = [p.read_text(encoding="utf-8").strip() for p in yaml_paths]
        combined = "\n---\n".join(parts)
    finally:
        if body.target_cluster and config is not None:
            cluster_targets.cleanup_kubeconfig(config.kubeconfig_path)
        shutil.rmtree(tmpdir, ignore_errors=True)

    return Response(
        content=combined,
        media_type="text/yaml; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="rhdp-dry-run-manifests.yaml"',
        },
    )


@router.get("/clusters")
def list_clusters(request: Request, _key=Depends(verify_api_key)):
    """List deploy-target clusters available to the requesting user.

    Returns ``allowed`` (is this user on the picker allowlist) and, when allowed,
    the configured target clusters. Non-allowlisted users get ``allowed: false``
    and an empty list, so the UI simply hides the picker. Selection is also
    enforced server-side on the deploy endpoints, so this is not the only gate.
    """
    allowed = identity.is_picker_allowed(request)
    return {
        "allowed": allowed,
        "user": identity.get_user_email(request),
        "clusters": cluster_targets.list_target_clusters() if allowed else [],
    }


@router.get("/deploy/status/{job_id}", response_model=JobResponse)
def deploy_status(job_id: str):
    job = jobs.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return JobResponse(
        job_id=job.job_id,
        status=JobStatus(job.status.value),
        progress=job.progress,
        message=job.message,
        error=job.error,
        results=[DeploymentResultResponse(**r) for r in (job.results or [])],
        log_file=os.path.basename(job.log_path) if job.log_path else None,
    )


@router.get("/deploy/stream/{job_id}")
async def deploy_stream(job_id: str, _key=Depends(verify_api_key)):
    job = jobs.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return EventSourceResponse(jobs.event_generator(job_id))


@router.websocket("/deploy/ws/{job_id}")
async def deploy_ws(websocket: WebSocket, job_id: str):
    """WebSocket endpoint for bidirectional deploy progress.

    Server sends JSON status updates. Client can send:
      {"command": "cancel"}
      {"command": "pause"}
      {"command": "resume"}

    Requires API key authentication via query parameter when RHDP_API_KEY is set.
    """
    import hmac
    import json as _json

    from api.auth import _get_required_key

    # Check API key auth before accepting connection
    required_key = _get_required_key()
    if required_key is not None:
        # Extract API key from query parameters
        query_params = dict(websocket.query_params)
        provided_key = query_params.get("api_key")
        if not provided_key or not hmac.compare_digest(provided_key, required_key):
            await websocket.close(code=4003, reason="Invalid or missing API key")
            return

    job = jobs.get_job(job_id)
    if not job:
        await websocket.close(code=4004, reason="Job not found")
        return
    await websocket.accept()

    async def _send_updates():
        await websocket.send_json(jobs._job_to_dict(job))
        while job.status in (jobs.Status.pending, jobs.Status.running, jobs.Status.paused):
            try:
                data = await asyncio.wait_for(job._events.get(), timeout=30)
                await websocket.send_json(data)
                if data.get("status") in (jobs.Status.completed.value, jobs.Status.failed.value, jobs.Status.cancelled.value):
                    return
            except TimeoutError:
                await websocket.send_json({"keepalive": True})
        await websocket.send_json(jobs._job_to_dict(job))

    async def _recv_commands():
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    msg = _json.loads(raw)
                except _json.JSONDecodeError:
                    continue
                cmd = msg.get("command")
                if cmd == "cancel":
                    jobs.request_cancel(job_id)
                elif cmd == "pause":
                    jobs.request_pause(job_id)
                elif cmd == "resume":
                    jobs.request_resume(job_id)
        except WebSocketDisconnect:
            pass

    send_task = asyncio.create_task(_send_updates())
    recv_task = asyncio.create_task(_recv_commands())
    done, pending = await asyncio.wait(
        {send_task, recv_task}, return_when=asyncio.FIRST_COMPLETED,
    )
    for t in pending:
        t.cancel()
    try:
        await websocket.close()
    except Exception:
        pass


@router.post("/deploy/cancel/{job_id}")
def deploy_cancel(job_id: str, _key=Depends(verify_api_key)):
    """Cancel a running deployment."""
    if jobs.request_cancel(job_id):
        return {"message": f"Cancel requested for job {job_id}"}
    raise HTTPException(404, "Job not found or not cancellable")


@router.post("/deploy/pause/{job_id}")
def deploy_pause(job_id: str, _key=Depends(verify_api_key)):
    """Pause a running deployment."""
    if jobs.request_pause(job_id):
        return {"message": f"Paused job {job_id}"}
    raise HTTPException(404, "Job not found or not running")


@router.post("/deploy/resume/{job_id}")
def deploy_resume(job_id: str, _key=Depends(verify_api_key)):
    """Resume a paused deployment."""
    if jobs.request_resume(job_id):
        return {"message": f"Resumed job {job_id}"}
    raise HTTPException(404, "Job not found or not paused")


@router.get("/deploy/results", response_model=list[DeploymentResultResponse])
def get_deploy_results():
    return [_result_to_response(r) for r in _deployment_results]


@router.post("/deploy/results/delete")
def delete_deploy_results(body: DeleteResultsRequest, _key=Depends(verify_api_key)):
    """Delete deployment result rows from the dashboard view.

    Removes matching rows from the in-memory results and the persisted snapshot.
    This is a view-only cleanup — it does NOT undeploy or destroy anything on the
    cluster. Rows are matched by (ci, namespace), the key results are stored under.
    """
    global _deployment_results
    refs = {(i.ci, i.namespace) for i in body.items}
    with _state_lock:
        before = len(_deployment_results)
        _deployment_results = [r for r in _deployment_results if (r.ci, r.namespace) not in refs]
        deleted = before - len(_deployment_results)
        _save_results(_deployment_results)
    return {"deleted": deleted, "remaining": len(_deployment_results)}


@router.post("/deploy/preview")
def deploy_preview(body: DeployRequest = DeployRequest(), _key=Depends(verify_api_key)):  # type: ignore
    """Preview the deployment plan, including multi-region user splits.

    Returns a plan without deploying anything. Useful for reviewing
    how users will be distributed across AWS regions before committing.
    """
    if not _schedules:
        raise HTTPException(400, "No schedules loaded. Upload a CSV first.")
    schedules = _filter_schedules(body.ci_filter)

    items = []
    for s in schedules:
        regions = [r.strip().replace("_", "-") for r in s.aws_regions.split(",") if r.strip()]
        total_users = s.users or 0
        item: dict = {
            "ci_name": s.ci_name,
            "ci": s.ci,
            "namespace": s.namespace,
            "users": s.users,
            "instances": s.instances,
            "count": s.count,
            "is_multi_asset": s.is_multi_asset,
        }
        if len(regions) >= 2:
            base_count = total_users // len(regions) if total_users else 0
            remainder = total_users % len(regions) if total_users else 0
            region_plan = []
            for idx, region in enumerate(regions):
                region_count = base_count + (1 if idx < remainder else 0)
                region_plan.append({"region": region, "users": region_count})
            item["multi_region"] = True
            item["regions"] = region_plan
        elif len(regions) == 1:
            item["multi_region"] = False
            item["regions"] = [{"region": regions[0], "users": total_users}]
        else:
            item["multi_region"] = False
            item["regions"] = []
        items.append(item)
    return {"schedules": items}


@router.post("/deploy/retry", response_model=JobResponse)
@_rate_limit("10/minute")
async def deploy_retry(request: Request, body: RetryRequest, _key=Depends(verify_api_key)):
    """Re-deploy specific workshops by CI name (typically failed ones)."""
    if not _schedules:
        raise HTTPException(400, "No schedules loaded. Upload a CSV first.")

    ci_name_set = set(body.ci_names)
    matching = [s for s in _schedules if s.ci_name in ci_name_set]
    if not matching:
        raise HTTPException(404, f"No schedules match the provided CI names: {body.ci_names}")

    target = request.headers.get("X-RHDP-Target-Cluster")
    if target:
        identity.require_picker_access(request)

    # Pre-deploy num_users limit check (live deploys only) - same as main deploy
    def preflight():
        config_check = _get_config(target_cluster=target)
        try:
            ci_cache: dict[str, dict | None] = {}
            limit_errors = []
            for s in matching:
                if s.users is not None and s.users > 0:
                    if s.ci not in ci_cache:
                        ci_cache[s.ci] = get_catalog_item_num_users_limit(s.ci, config_check)
                    info = ci_cache[s.ci]
                    if info and info.get("maximum") is not None and s.users > info["maximum"]:
                        limit_errors.append(
                            f"{s.ci_name} ({s.ci}): {s.users} users exceeds catalog maximum of {info['maximum']}"
                        )
            if limit_errors:
                raise HTTPException(
                    400,
                    f"num_users limit exceeded: {'; '.join(limit_errors)}"
                )

        finally:
            if target:
                cluster_targets.cleanup_kubeconfig(config_check.kubeconfig_path)

    if not body.dry_run:
        await asyncio.to_thread(preflight)

    job = jobs.create_job()

    async def _run():
        global _deploy_log_path
        handler, log_path = start_log_capture("deploy-retry", job.job_id)
        config = None
        try:
            config = await asyncio.to_thread(_get_config,
                dry_run=body.dry_run,
                resource_lock=body.resource_lock,
                enable_resource_pools=body.enable_resource_pools,
                white_glove=body.white_glove,
                redirect=body.redirect,
                target_cluster=target,
            )
            jobs.update_job(job.job_id, status=jobs.Status.running, message=f"Retrying {len(matching)} deployment(s)")

            results = []
            for i, s in enumerate(matching):
                result = await asyncio.to_thread(
                    process_schedule, s, config, _asset_passwords
                )
                results.append(result)
                pct = int((i + 1) / len(matching) * 100)
                jobs.update_job(
                    job.job_id, progress=pct,
                    message=f"Retried {result.ci_name}: {result.status}",
                )
                if not config.dry_run and len(matching) > 1:
                    await asyncio.sleep(1)

            # Update global results: replace matching entries, keep the rest
            global _deployment_results
            result_map = {r.ci_name: r for r in results}
            with _state_lock:
                _deployment_results = [
                    result_map.get(r.ci_name, r) for r in _deployment_results
                ] + [r for r in results if r.ci_name not in {dr.ci_name for dr in _deployment_results}]
                _deploy_log_path = log_path
            _save_results(_deployment_results)
            jobs.update_job(
                job.job_id,
                status=jobs.Status.completed,
                progress=100,
                message=f"Retry completed: {len(results)} deployment(s)",
                results=[asdict(r) for r in results],
                log_path=log_path,
            )
        except Exception as exc:
            with _state_lock:
                _deploy_log_path = log_path
            jobs.update_job(
                job.job_id,
                status=jobs.Status.failed,
                error=str(exc),
                message=f"Retry failed: {exc}",
                log_path=log_path,
            )
        finally:
            if target and config:
                cluster_targets.cleanup_kubeconfig(config.kubeconfig_path)
            stop_log_capture(handler)

    asyncio.create_task(_run())
    return JobResponse(job_id=job.job_id, status=JobStatus(job.status.value), progress=job.progress)


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

@router.post("/operations/lock", response_model=OperationResponse)
@_rate_limit("10/minute")
def op_lock(request: Request, body: LockRequest = LockRequest(), _key=Depends(verify_api_key), config=Depends(_request_config)):
    if not _schedules:
        raise HTTPException(400, "No schedules loaded.")
    schedules = _filter_schedules(body.ci_filter)
    lock_workshops(schedules, config)
    return OperationResponse(success=True, message=f"Locked {len(schedules)} schedule(s)")


@router.post("/operations/unlock", response_model=OperationResponse)
@_rate_limit("10/minute")
def op_unlock(request: Request, body: LockRequest = LockRequest(), _key=Depends(verify_api_key), config=Depends(_request_config)):
    if not _schedules:
        raise HTTPException(400, "No schedules loaded.")
    schedules = _filter_schedules(body.ci_filter)
    unlock_workshops(schedules, config)
    return OperationResponse(success=True, message=f"Unlocked {len(schedules)} schedule(s)")


@router.post("/operations/extend-stop", response_model=OperationResponse)
@_rate_limit("10/minute")
def op_extend_stop(request: Request, body: ExtendRequest, _key=Depends(verify_api_key), config=Depends(_request_config)):
    if not _schedules:
        raise HTTPException(400, "No schedules loaded.")
    if body.days == 0 and body.hours == 0:
        raise HTTPException(400, "Must specify days and/or hours > 0")
    schedules = _filter_schedules(body.ci_filter)
    extend_stop_time(schedules, config, body.days, body.hours)
    return OperationResponse(
        success=True,
        message=f"Extended stop time by {body.days}d {body.hours}h for {len(schedules)} schedule(s)",
    )


@router.post("/operations/extend-destroy", response_model=OperationResponse)
@_rate_limit("10/minute")
def op_extend_destroy(request: Request, body: ExtendRequest, _key=Depends(verify_api_key), config=Depends(_request_config)):
    if not _schedules:
        raise HTTPException(400, "No schedules loaded.")
    if body.days == 0 and body.hours == 0:
        raise HTTPException(400, "Must specify days and/or hours > 0")
    schedules = _filter_schedules(body.ci_filter)
    extend_destroy_time(schedules, config, body.days, body.hours)
    return OperationResponse(
        success=True,
        message=f"Extended destroy time by {body.days}d {body.hours}h for {len(schedules)} schedule(s)",
    )


@router.post("/operations/disable-autostop", response_model=OperationResponse)
@_rate_limit("10/minute")
def op_disable_autostop(request: Request, body: DisableAutostopRequest = DisableAutostopRequest(), _key=Depends(verify_api_key), config=Depends(_request_config)):
    if not _schedules:
        raise HTTPException(400, "No schedules loaded.")
    schedules = _filter_schedules(body.ci_filter)
    patched = disable_autostop(schedules, config)
    return OperationResponse(
        success=patched > 0 or config.dry_run,
        message=f"Disabled auto-stop: {patched} resource(s) patched across {len(schedules)} schedule(s)",
    )


@router.post("/operations/scale", response_model=OperationResponse)
@_rate_limit("10/minute")
def op_scale(request: Request, body: ScaleRequest, _key=Depends(verify_api_key), config=Depends(_request_config)):
    if not _schedules:
        raise HTTPException(400, "No schedules loaded.")
    schedules = _filter_schedules(body.ci_filter)
    scale_workshops(schedules, config, body.target_count)
    return OperationResponse(
        success=True,
        message=f"Scaled {len(schedules)} schedule(s) to count={body.target_count}",
    )


@router.post("/operations/showroom-cleanup", response_model=OperationResponse)
@_rate_limit("10/minute")
def op_showroom_cleanup(request: Request, body: ShowroomCleanupRequest = ShowroomCleanupRequest(), _key=Depends(verify_api_key), config=Depends(_request_config)):
    if not _schedules:
        raise HTTPException(400, "No schedules loaded.")
    schedules = _filter_schedules(body.ci_filter)
    cleaned, failed, failed_details = teardown_showroom(schedules, config)
    success = failed == 0 or config.dry_run
    details = failed_details if failed_details else []
    return OperationResponse(
        success=success,
        message=f"Showroom cleanup: {cleaned} removed, {failed} failed across {len(schedules)} schedule(s)",
        details=details,
    )


@router.post("/operations/showroom-health", response_model=OperationResponse)
@_rate_limit("10/minute")
def op_showroom_health(request: Request, body: ShowroomHealthRequest = ShowroomHealthRequest(), _key=Depends(verify_api_key), config=Depends(_request_config)):
    if not _schedules:
        raise HTTPException(400, "No schedules loaded.")
    schedules = _filter_schedules(body.ci_filter)
    results = []
    for s in schedules:
        health = check_showroom_health(s, config)
        results.append(f"{s.ci_name}: {health['status']} ({health['url'] or 'no route'})")
    healthy_count = sum(1 for r in results if "healthy" in r)
    return OperationResponse(
        success=True,
        message=f"Showroom health: {healthy_count}/{len(schedules)} healthy",
        details=results,
    )


@router.post("/operations/showroom-preflight", response_model=OperationResponse)
@_rate_limit("5/minute")
def op_showroom_preflight(request: Request, body: ShowroomPreflightRequest = ShowroomPreflightRequest(), _key=Depends(verify_api_key)):
    """Run Demolition preflight checks against deployed workshop URLs.

    Uses deployment results' landing page URLs to verify workshops are browser-accessible.
    Falls back to the url field when no landing page URL is available.
    """
    if not _deployment_results:
        raise HTTPException(400, "No deployment results available. Deploy first, then run preflight.")
    results_to_check = _deployment_results
    if body.ci_filter:
        results_to_check = [r for r in results_to_check if r.ci == body.ci_filter or r.ci_name == body.ci_filter]
    if not results_to_check:
        raise HTTPException(400, f"No deployment results match filter '{body.ci_filter}'.")

    urls = []
    for r in results_to_check:
        target_url = r.url
        urls.append({"ci_name": r.ci_name, "url": target_url, "password": r.password})

    preflight_results = run_demolition_preflight(urls)
    details = []
    pass_count = 0
    for pr in preflight_results:
        status_label = pr["status"].upper()
        details.append(f"{pr['ci_name']}: {status_label} — {pr['message'][:200]}")
        if pr["status"] == "pass":
            pass_count += 1

    total = len(preflight_results)
    all_ok = all(pr["status"] in ("pass", "skipped") for pr in preflight_results)
    return OperationResponse(
        success=all_ok,
        message=f"Demolition preflight: {pass_count}/{total} passed",
        details=details,
    )


@router.post("/operations/showroom-applicationset")
@_rate_limit("10/minute")
def op_showroom_applicationset(request: Request, body: ShowroomAppSetRequest = ShowroomAppSetRequest(), _key=Depends(verify_api_key), config=Depends(_request_config)):  # type: ignore
    if not _schedules:
        raise HTTPException(400, "No schedules loaded.")
    schedules = _filter_schedules(body.ci_filter)
    yamls = []
    for s in schedules:
        if s.showroom_repo:
            appset = generate_showroom_applicationset(s, config, seat_count=body.seat_count)
            if appset:
                yamls.append(appset)
    if not yamls:
        return OperationResponse(success=False, message="No schedules have Showroom repos configured.")
    combined = "\n---\n".join(yamls)
    return OperationResponse(
        success=True,
        message=f"Generated {len(yamls)} ApplicationSet(s)",
        details=[combined],
    )


@router.post("/operations/update-passwords", response_model=OperationResponse)
@_rate_limit("10/minute")
def op_update_passwords(request: Request, body: LockRequest = LockRequest(), _key=Depends(verify_api_key), config=Depends(_request_config)):
    if not _schedules:
        raise HTTPException(400, "No schedules loaded.")
    schedules = _filter_schedules(body.ci_filter)
    updated = update_passwords(schedules, config)
    return OperationResponse(
        success=True,
        message=f"Updated passwords for {updated} workshop(s)",
    )


@router.post("/operations/import-namespace")
@_rate_limit("10/minute")
def op_import_namespace(request: Request, namespace: str, _key=Depends(verify_api_key), config=Depends(_request_config)):
    _validate_namespace(namespace)
    import tempfile
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, encoding="utf-8")
    tmp.close()
    rows = import_namespace_to_csv(namespace, tmp.name, config)
    if not rows:
        os.unlink(tmp.name)
        raise HTTPException(404, f"No workshops found in namespace {namespace}")

    def _iter():
        with open(tmp.name) as f:
            yield f.read()
        os.unlink(tmp.name)

    return StreamingResponse(
        _iter(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=imported_{namespace}.csv"},
    )


# ---------------------------------------------------------------------------
# QA
# ---------------------------------------------------------------------------

@router.get("/qa/namespaces")
def qa_namespaces():
    """Return unique namespaces from loaded schedules for the QA namespace selector."""
    if not _schedules:
        # Return common namespaces even if no schedules loaded
        return [
            "user-bbethell-redhat-com",
            "user-vaguiler-redhat-com",
            "user-yvarbev-redhat-com",
        ]
    return list(dict.fromkeys(s.namespace for s in _schedules))


@router.post("/qa/run")
@_rate_limit("10/minute")
def qa_run(request: Request, body: QARequest = QARequest(), _key=Depends(verify_api_key), config=Depends(_request_config)):  # type: ignore
    global _qa_results, _qa_log_path
    if not _schedules:
        raise HTTPException(400, "No schedules loaded.")


    # Support multiple namespaces for faster targeted QA
    if body.namespaces:
        namespaces = body.namespaces
    elif body.namespace:
        # Support comma-separated namespaces in single field for backward compat
        namespaces = [ns.strip() for ns in body.namespace.split(",") if ns.strip()]
    else:
        namespaces = list(dict.fromkeys(s.namespace for s in _schedules))

    handler, log_path = start_log_capture("qa")
    temp_csv_paths: list[str] = []
    try:
        all_qa1: list[dict] = []
        all_qa2: list[dict] = []
        all_qa3: list[dict] = []

        for ns in namespaces:
            # Always write a fresh temp CSV from in-memory schedules so that
            # UI edits (changed dates, users, etc.) are reflected in QA checks.
            temp_path = _write_qa_csv_for_namespace(ns)
            temp_csv_paths.append(temp_path)

            if body.type.value in ("1", "both", "all"):
                all_qa1.extend(qa1_verify_setup(temp_path, ns, config))
            if body.type.value in ("2", "both", "all"):
                all_qa2.extend(qa2_verify_deployment_status(temp_path, ns, config))

        # QA3 runs once on full CSV (not namespace-specific)
        if body.type.value in ("3", "all") and temp_csv_paths:
            all_qa3.extend(qa3_verify_catalog_items_exist(temp_csv_paths[0], config))

        if body.type.value == "1":
            all_raw = _dedup_qa_results(all_qa1)
        elif body.type.value == "2":
            all_raw = all_qa2
        elif body.type.value == "3":
            all_raw = all_qa3
        elif body.type.value == "both":
            all_raw = _merge_qa1_qa2(all_qa1, all_qa2)
        else:  # "all"
            merged = _merge_qa1_qa2(all_qa1, all_qa2)
            all_raw = merged + all_qa3

        all_raw = [_normalize_qa_result_dict(r) for r in all_raw]
        all_results = [QAResultItem(**r) for r in all_raw]
        with _state_lock:
            _qa_results = all_results
            _qa_log_path = log_path
        return {
            "count": len(all_results),
            "results": all_results,
            "log_file": os.path.basename(log_path),
        }
    finally:
        stop_log_capture(handler)
        for tp in temp_csv_paths:
            Path(tp).unlink(missing_ok=True)


@router.get("/qa/results")
def qa_get_results():
    return {"count": len(_qa_results), "results": _qa_results}


@router.post("/qa/destroy-check", response_model=DestroyCheckResponse)
@_rate_limit("10/minute")
def qa_destroy_check_endpoint(request: Request, body: DestroyCheckRequest = DestroyCheckRequest(), _key=Depends(verify_api_key), config=Depends(_request_config)):  # type: ignore
    """Read-only check whether deployments have been properly destroyed/stopped."""
    global _destroy_check_results
    if not _schedules:
        raise HTTPException(400, "No schedules loaded.")

    all_results: list[dict] = []
    temp_csv_paths: list[str] = []

    if body.namespace:
        namespaces = [body.namespace]
    else:
        namespaces = list(dict.fromkeys(s.namespace for s in _schedules))

    handler, log_path = start_log_capture("destroy-check")
    try:
        for ns in namespaces:
            temp_path = _write_qa_csv_for_namespace(ns)
            temp_csv_paths.append(temp_path)
            r = qa_destroy_check(temp_path, ns, config)
            all_results.extend(r)

        with _state_lock:
            _destroy_check_results = all_results
        return DestroyCheckResponse(
            count=len(all_results),
            results=[DestroyCheckResult(**item) for item in all_results]
        )
    finally:
        stop_log_capture(handler)
        for tp in temp_csv_paths:
            Path(tp).unlink(missing_ok=True)


@router.get("/qa/destroy-check/results")
def qa_destroy_check_results():
    """Return stored destroy-check results."""
    return {"count": len(_destroy_check_results), "results": _destroy_check_results}


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------

@router.get("/logs")
def list_logs():
    """List available log files, newest first."""
    log_dir = get_log_dir()
    try:
        files = [f for f in os.listdir(log_dir) if f.endswith(".log")]
    except FileNotFoundError:
        files = []
    files.sort(reverse=True)
    return {"files": files}


_LOG_FILENAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_\-\.]*\.log$")


@router.get("/logs/{filename}")
def download_log(filename: str):
    """Download a specific log file."""
    if not _LOG_FILENAME_RE.match(filename):
        raise HTTPException(400, "Invalid filename: must be alphanumeric with .log extension")
    log_dir = get_log_dir()
    filepath = os.path.join(log_dir, filename)
    if not os.path.isfile(filepath):
        raise HTTPException(404, "Log file not found")
    return StreamingResponse(
        open(filepath, encoding="utf-8"),
        media_type="text/plain",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

@router.get("/export/results")
def export_results():
    if not _deployment_results:
        raise HTTPException(404, "No deployment results available.")

    output = io.StringIO()
    fieldnames = [
        "ci_name", "ci", "namespace", "guid", "url", "status",
        "provisioning_date", "auto_stop", "auto_destroy",
        "timestamp", "error_message", "showroom_url", "showroom_status",
        "password", "cluster_name", "cluster_capacity",
        "users", "instances",
        "log_url",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for r in _deployment_results:
        writer.writerow(asdict(r))

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=deployment_results.csv"},
    )


@router.get("/templates/schedule")
def download_template():
    """Download a CSV template with headers and an example row.

    Headers match ``read_csv_input`` in rhdp_flow.py (case-insensitive).
    """
    output = io.StringIO()
    fieldnames = [
        "CI Name",
        "CI",
        "Namespace",
        "Users",
        "Enable_workshop_interface",
        "Password",
        "Activity",
        "Purpose",
        "Workshop Name",
        "Provisioning Date (UTC)",
        "Auto-stop (UTC)",
        "Auto-destroy (UTC)",
        "Multi_Asset",
        "Asset_CIs",
        "Multi_Workshop_Name",
        "Concurrency",
        "Instances",
        "Salesforce IDs",
        "Salesforce_Type",
        "Count",
        "AWS_Region",
        "Redirect",
        "Catalog_Namespace",
        "Showroom_Repo",
        "Showroom_Ref",
        "Showroom_NoVNC",
        "Showroom_Zerotouch",
        "White_Glove",
        "Item_Type",
        "Cluster_CI",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerow({
        "CI Name": "Example Workshop",
        "CI": "vendor.workshop.prod",
        "Namespace": "user-ns",
        "Users": "30",
        "Enable_workshop_interface": "True",
        "Password": "changeme",
        "Activity": "Training",
        "Purpose": "Demo",
        "Workshop Name": "my-workshop",
        "Provisioning Date (UTC)": "15/03/2025 09:00",
        "Auto-stop (UTC)": "15/03/2025 17:00",
        "Auto-destroy (UTC)": "16/03/2025 09:00",
        "Multi_Asset": "",
        "Asset_CIs": "",
        "Multi_Workshop_Name": "",
        "Concurrency": "",
        "Instances": "",
        "Salesforce IDs": "",
        "Salesforce_Type": "",
        "Count": "",
        "AWS_Region": "",
        "Redirect": "",
        "Catalog_Namespace": "",
        "Showroom_Repo": "",
        "Showroom_Ref": "",
        "Showroom_NoVNC": "",
        "Showroom_Zerotouch": "",
        "White_Glove": "True",
        "Item_Type": "",
        "Cluster_CI": "",
    })
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=schedule_template.csv"},
    )


@router.get("/export/students")
def export_students():
    if not _qa_results:
        raise HTTPException(404, "No QA results available. Run QA first.")

    import tempfile
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".csv", delete=False, encoding="utf-8"
    )
    tmp.close()
    export_student_landing_page_csv([item.model_dump() for item in _qa_results], tmp.name)

    def _iter():
        with open(tmp.name) as f:
            yield f.read()
        os.unlink(tmp.name)

    return StreamingResponse(
        _iter(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=student_landing_page.csv"},
    )


@router.get("/schedules/export-for-labagator")
async def export_for_labagator(_key=Depends(verify_api_key)):
    """Export current Flow schedules as Labagator-compatible CSV."""
    if not _schedules:
        raise HTTPException(400, "No schedules loaded")

    output = io.StringIO()
    fieldnames = ["session_code", "title", "room", "session_date", "start_time", "end_time", "speakers", "topics"]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()

    for s in _schedules:
        # Parse Flow dates (DD/MM/YYYY HH:MM → YYYY-MM-DD, HH:MM)
        start_dt = datetime.strptime(s.provisioning_date, "%d/%m/%Y %H:%M")
        stop_dt = datetime.strptime(s.auto_stop, "%d/%m/%Y %H:%M")

        # Extract session code from CI name (assumes "CODE - Title" format)
        name_parts = s.ci_name.split(" - ", 1)
        session_code = name_parts[0] if len(name_parts) > 1 else s.ci_name
        title = name_parts[1] if len(name_parts) > 1 else ""

        row = {
            "session_code": session_code,
            "title": title,
            "room": "",
            "session_date": start_dt.strftime("%Y-%m-%d"),
            "start_time": start_dt.strftime("%H:%M"),
            "end_time": stop_dt.strftime("%H:%M"),
            "speakers": "",
            "topics": s.purpose or "",
        }
        writer.writerow(row)

    content = output.getvalue()
    return Response(
        content=content,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=flow-export-for-labagator.csv"},
    )


@router.get("/schedules/cluster-needs", response_model=ClusterNeedsResponse)
def get_cluster_needs(_key=Depends(verify_api_key), config=Depends(_request_config)):
    if not _schedules:
        raise HTTPException(400, "No schedules loaded.")
    refs = _tenant_refs(_schedules, config)
    # Workshop Manager sizes each dedicated pool from spec.count and
    # sandboxHost.max_placements. CSV row counts are not placement demand.
    return ClusterNeedsResponse(total_tenant_count=refs["total_tenant_count"])


@router.get("/schedules/tenant-cluster-refs")
def check_tenant_cluster_refs(_key=Depends(verify_api_key), config=Depends(_request_config)):
    """Check if tenant workshops have proper catalog cluster references.

    Queries CatalogItems to verify tenant_cluster configuration exists.
    Missing references cause immediate provision failures.

    Returns:
        Dict with missing_refs list and total_tenant_count
    """
    if not _schedules:
        raise HTTPException(400, "No schedules loaded.")

    try:
        from lib.tenant_cluster_capacity import check_tenant_cluster_references

        result = check_tenant_cluster_references(_schedules, env=_config_env(config))
        return result
    except ImportError as e:
        logger.warning(f"Tenant cluster reference check unavailable: {e}")
        raise HTTPException(503, "Tenant reference checking is unavailable") from e
    except Exception as e:
        logger.exception("Tenant cluster reference check failed")
        raise HTTPException(502, f"Tenant reference check failed: {e}") from e



@router.post("/schedules/check-pool-status")
def check_pool_status(
    body: dict,
    _key=Depends(verify_api_key),
    config=Depends(_request_config),
):
    """Check the current cluster status of a list of TenantClusterPool names.

    Returns per-pool: exists, enabled, available_clusters, action_preview.
    Used by the frontend to show what will happen before the user clicks Apply.
    """
    cluster_cis: list[str] = body.get("cluster_cis", [])
    results = []
    for ci in cluster_cis:
        try:
            proc = subprocess.run(
                ["oc", "get", "tenantclusterpool", ci, "-n", "shared-clusters", "-o", "json"],
                capture_output=True, text=True, timeout=30, env=_config_env(config),
            )
            if proc.returncode != 0:
                if "(NotFound)" not in proc.stderr:
                    raise RuntimeError(proc.stderr.strip() or "Pool lookup failed")
                results.append({
                    "name": ci,
                    "exists": False,
                    "enabled": False,
                    "available_clusters": 0,
                    "action_preview": "create",
                })
                continue
            pool = json.loads(proc.stdout)
            spec = pool.get("spec", {})
            clusters = pool.get("status", {}).get("clusters", [])
            enabled = spec.get("enabled", False)
            available = sum(1 for c in clusters if c.get("sandboxApiState") == "available")
            min_avail = spec.get("minAvailableSandboxPlacements", 0)

            action = "already_exists"

            results.append({
                "name": ci,
                "exists": True,
                "enabled": enabled,
                "available_clusters": available,
                "action_preview": action,
            })
        except Exception as exc:
            results.append({
                "name": ci,
                "exists": False,
                "enabled": False,
                "available_clusters": 0,
                "action_preview": "error",
                "error": str(exc),
            })
    return {"results": results}


@router.post("/schedules/create-tenant-cluster-pools")
def create_tenant_cluster_pools(
    body: CreateTenantClusterPoolsRequest,
    _key=Depends(verify_api_key),
    config=Depends(_request_config),
):
    """Generate (and optionally apply) TenantClusterPool CRDs for missing pools.

    Generates a TenantClusterPool manifest for each cluster CI. If apply_to_cluster
    is True, applies them via `oc apply -f -`. Otherwise returns the YAML only.

    Returns: {yaml, applied, results, count}
    """
    import yaml as _yaml

    yamls = []
    for cluster_ci in body.cluster_cis:
        # Derive lab annotation: strip namespace prefix and -cluster.<suffix>
        parts = cluster_ci.split(".", 1)
        ci_tail = parts[1] if len(parts) > 1 else cluster_ci
        lab = re.sub(r"-cluster\.[^.]+$", "", ci_tail)

        # Derive purpose from CI stage suffix (.event → events, .prod → prod, else dev)
        ci_suffix = cluster_ci.rsplit(".", 1)[-1] if "." in cluster_ci else ""
        if ci_suffix == "event":
            purpose = "events"
        elif ci_suffix == "prod":
            purpose = "prod"
        else:
            purpose = "dev"

        pool = {
            "apiVersion": "babylon.gpte.redhat.com/v1",
            "kind": "TenantClusterPool",
            "metadata": {
                "name": cluster_ci,
                "namespace": "shared-clusters",
                "labels": {"flow.demo.redhat.com/managed": "true"},
            },
            "spec": {
                "clusterProvisioning": {
                    "provider": {
                        "name": cluster_ci,
                        "parameterValues": {"purpose": "Tenant Cluster"},
                    }
                },
                "enabled": body.enabled,
                "maxClusters": body.max_clusters,
                "minAvailableSandboxPlacements": body.min_available_sandbox_placements,
                "minClusters": body.min_clusters,
                "sandboxHost": {
                    "annotations": {
                        "cloud": body.cloud,
                        "environment_level": body.environment_level,
                        "lab": lab,
                        "purpose": purpose,
                    },
                    "max_cpu_usage_percentage": 90,
                    "max_memory_usage_percentage": 85,
                    "max_placements": body.max_placements,
                    "quota_required": False,
                },
            },
        }
        yamls.append(pool)

    combined_yaml = "---\n".join(_yaml.dump(p, default_flow_style=False) for p in yamls)

    def _get_existing_pool_spec(pool_name: str) -> dict | None:
        """Return the spec of an existing TenantClusterPool, or None if not found."""
        proc = subprocess.run(
            ["oc", "get", "tenantclusterpool", pool_name,
             "-n", "shared-clusters", "-o", "json"],
            capture_output=True, text=True, timeout=30, env=_config_env(config),
        )
        if proc.returncode != 0:
            if "(NotFound)" in proc.stderr:
                return None
            raise RuntimeError(proc.stderr.strip() or "Pool lookup failed")
        return json.loads(proc.stdout).get("spec", {})

    results = []
    if body.apply_to_cluster:
        for pool in yamls:
            pool_name = pool["metadata"]["name"]
            try:
                existing_spec = _get_existing_pool_spec(pool_name)

                if existing_spec is not None:
                    # Reference templates need not be enabled or hold capacity.
                    # Do not activate/resize an existing shared pool as a side effect.
                    results.append({
                        "name": pool_name, "success": True, "action": "already_exists",
                        "output": "Reference pool already exists; left unchanged.", "error": "",
                    })
                else:
                    # Pool does not exist — create it
                    pool_yaml = _yaml.dump(pool, default_flow_style=False)
                    proc = subprocess.run(
                        ["oc", "apply", "-f", "-"],
                        input=pool_yaml,
                        capture_output=True, text=True, timeout=30, env=_config_env(config),
                    )
                    results.append({
                        "name": pool_name,
                        "success": proc.returncode == 0,
                        "action": "created",
                        "output": proc.stdout.strip(),
                        "error": proc.stderr.strip() if proc.returncode != 0 else "",
                    })
            except Exception as exc:
                results.append({
                    "name": pool_name,
                    "success": False,
                    "action": "created",
                    "output": "",
                    "error": str(exc),
                })

    return {
        "yaml": combined_yaml,
        "applied": body.apply_to_cluster,
        "results": results,
        "count": len(yamls),
    }
