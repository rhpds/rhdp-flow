"""Tenant cluster capacity checking for RHDP-Flow.

Provides read-only capacity warnings for workshops deploying to existing tenant clusters.
Only applies to catalog items ending in -tenant (existing clusters).
Fresh cluster deployments are skipped.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("rhdp_flow.tenant_cluster_capacity")


@dataclass
class ClusterCapacity:
    """Cluster capacity information with dual metrics.

    Tracks both pool saturation (cluster allocation) and placement capacity
    (actual workshop slot utilization).
    """
    cluster_name: str
    total_clusters: int
    available_clusters: int
    pool_saturation_percent: int  # Renamed from utilization_percent
    max_placements_per_cluster: int
    workshops_deployed: int
    placement_capacity_percent: int

    @property
    def status(self) -> str:
        """Return status: healthy, warning, or critical.

        Uses placement capacity as primary metric when available,
        falls back to pool saturation.
        """
        # Use placement capacity if workshop count is available
        metric = self.placement_capacity_percent if self.workshops_deployed >= 0 else self.pool_saturation_percent

        if metric >= 90:
            return "critical"
        elif metric >= 70:
            return "warning"
        return "healthy"

    @property
    def message(self) -> str:
        """Return human-readable message showing both metrics."""
        pool_msg = f"Pool: {self.pool_saturation_percent}% saturated ({self.total_clusters - self.available_clusters}/{self.total_clusters} clusters occupied)"
        placement_msg = f"Placements: {self.placement_capacity_percent}% utilized ({self.workshops_deployed}/{self.total_clusters * self.max_placements_per_cluster} workshops)"

        if self.status == "critical":
            return f"CRITICAL: Cluster {self.cluster_name} at capacity - {placement_msg}, {pool_msg}"
        elif self.status == "warning":
            return f"WARNING: Cluster {self.cluster_name} nearing capacity - {placement_msg}, {pool_msg}"
        return f"OK: Cluster {self.cluster_name} has capacity - {placement_msg}, {pool_msg}"


def is_tenant_catalog_item(ci: str) -> bool:
    """Check if catalog item is a tenant variant (deploys to existing cluster)."""
    return ci.endswith("-tenant")


def check_cluster_capacity(catalog_item: str, namespace: str = None) -> ClusterCapacity | None:
    """
    Check tenant cluster capacity for a catalog item (read-only).

    Args:
        catalog_item: Catalog item name (e.g., "workshop.prod-tenant")
        namespace: Optional namespace hint

    Returns:
        ClusterCapacity if cluster found and capacity determined, None otherwise

    Note:
        This function is safe to fail - if API is unavailable or cluster not found,
        it returns None and deployment proceeds without warnings.
    """
    # Skip if not a tenant variant
    if not is_tenant_catalog_item(catalog_item):
        logger.debug(f"Skipping capacity check for non-tenant catalog item: {catalog_item}")
        return None

    try:
        # Import kubernetes client only when needed
        try:
            from kubernetes import client, config
        except ImportError:
            logger.warning("kubernetes client not installed - skipping capacity check")
            return None

        # Try to load kube config
        try:
            config.load_incluster_config()
        except config.ConfigException:
            try:
                config.load_kube_config()
            except config.ConfigException:
                logger.warning("Could not load kubernetes config - skipping capacity check")
                return None

        # Query TenantClusterPools (read-only)
        api = client.CustomObjectsApi()

        try:
            # List all TenantClusterPools (cluster-wide query)
            pools = api.list_cluster_custom_object(
                group="babylon.gpte.redhat.com",
                version="v1",
                plural="tenantclusterpools"
            )
        except client.exceptions.ApiException as e:
            if e.status == 404:
                logger.debug("TenantClusterPool CRD not found - skipping capacity check")
            else:
                logger.warning(f"Error querying TenantClusterPools: {e}")
            return None

        # Find matching pool (simple heuristic: first pool with available clusters)
        # In production, this would use resource claim mapping like babylon ops does
        # For now, we just check if ANY tenant cluster pool exists and report its capacity
        for pool in pools.get("items", []):
            pool_name = pool.get("metadata", {}).get("name", "")
            pool_namespace = pool.get("metadata", {}).get("namespace", "")
            status = pool.get("status", {})
            spec = pool.get("spec", {})
            clusters = status.get("clusters", [])

            if not clusters:
                continue

            # Pool saturation (cluster allocation)
            total = len(clusters)
            available = sum(1 for c in clusters if c.get("sandboxApiState") == "available")
            occupied = total - available
            pool_saturation_percent = int((occupied / total) * 100) if total > 0 else 0

            # Placement capacity (workshop slots)
            max_placements = spec.get("sandboxHost", {}).get("max_placements", 50)

            # Count workshops in this pool (ResourceClaims with tenantClusterPoolName label)
            workshops_deployed = 0
            try:
                core_v1 = client.CoreV1Api()
                resource_claims = api.list_cluster_custom_object(
                    group="poolboy.gpte.redhat.com",
                    version="v1",
                    plural="resourceclaims",
                    label_selector=f"babylon.gpte.redhat.com/tenantClusterPoolName={pool_name}"
                )
                workshops_deployed = len(resource_claims.get("items", []))
            except Exception as e:
                logger.debug(f"Could not count workshops for pool {pool_name}: {e}")
                # Non-fatal - continue with workshops_deployed = 0

            max_total_placements = total * max_placements
            placement_capacity_percent = int((workshops_deployed / max_total_placements) * 100) if max_total_placements > 0 else 0

            logger.info(
                f"Found tenant cluster pool {pool_name}: "
                f"Pool saturation: {pool_saturation_percent}% ({occupied}/{total} clusters), "
                f"Placement capacity: {placement_capacity_percent}% ({workshops_deployed}/{max_total_placements} workshops)"
            )

            return ClusterCapacity(
                cluster_name=pool_name,
                total_clusters=total,
                available_clusters=available,
                pool_saturation_percent=pool_saturation_percent,
                max_placements_per_cluster=max_placements,
                workshops_deployed=workshops_deployed,
                placement_capacity_percent=placement_capacity_percent
            )

        logger.debug(f"No tenant cluster pools found for {catalog_item}")
        return None

    except Exception as e:
        # Graceful failure - don't block deployments if capacity check fails
        logger.warning(f"Cluster capacity check failed (non-blocking): {e}")
        return None


def check_schedules_capacity(schedules: list[Any], ignore_warnings: bool = False) -> dict[str, Any]:
    """
    Check capacity for all tenant catalog items in schedules (read-only).

    Args:
        schedules: List of WorkshopSchedule objects
        ignore_warnings: If True, skip capacity checks

    Returns:
        Dict with:
        - warnings: List of capacity warnings
        - errors: List of critical capacity errors
        - checked_count: Number of tenant items checked
        - capacity_info: Dict mapping CI name to ClusterCapacity
    """
    if ignore_warnings:
        logger.info("Cluster capacity warnings ignored by user")
        return {
            "warnings": [],
            "errors": [],
            "checked_count": 0,
            "capacity_info": {}
        }

    warnings = []
    errors = []
    checked_count = 0
    capacity_info = {}

    for schedule in schedules:
        ci = schedule.ci

        if not is_tenant_catalog_item(ci):
            continue

        checked_count += 1
        capacity = check_cluster_capacity(ci, schedule.namespace)

        if capacity:
            capacity_info[schedule.ci_name] = capacity

            if capacity.status == "critical":
                errors.append({
                    "ci_name": schedule.ci_name,
                    "ci": ci,
                    "message": capacity.message,
                    "pool_saturation": capacity.pool_saturation_percent,
                    "placement_capacity": capacity.placement_capacity_percent
                })
            elif capacity.status == "warning":
                warnings.append({
                    "ci_name": schedule.ci_name,
                    "ci": ci,
                    "message": capacity.message,
                    "pool_saturation": capacity.pool_saturation_percent,
                    "placement_capacity": capacity.placement_capacity_percent
                })

    return {
        "warnings": warnings,
        "errors": errors,
        "checked_count": checked_count,
        "capacity_info": capacity_info
    }


def calculate_cluster_needs(schedules: list[Any]) -> dict[str, Any]:
    """
    Calculate how many cluster CIs are needed for tenant workshops.

    Groups tenant schedules by their cluster CI, counts tenants, queries pool
    capacity, and calculates cluster deficit.

    Args:
        schedules: List of WorkshopSchedule objects

    Returns:
        Dict with:
        - needs: List of dicts with cluster_ci, tenant_count, capacity_per_cluster,
                 clusters_needed, clusters_in_csv, deficit
        - total_tenant_count: Total tenant workshops
        - total_deficit: Total cluster shortage across all tenant types
    """
    import math

    needs = []
    total_tenant_count = 0
    total_deficit = 0

    # Group tenants by their detected cluster CI
    tenant_groups = {}  # cluster_ci -> list of tenant schedules
    cluster_counts = {}  # cluster_ci -> count of cluster rows in CSV

    for schedule in schedules:
        if schedule.is_tenant and schedule.detected_cluster_ci:
            cluster_ci = schedule.detected_cluster_ci
            if cluster_ci not in tenant_groups:
                tenant_groups[cluster_ci] = []
            tenant_groups[cluster_ci].append(schedule)
            total_tenant_count += 1
        elif schedule.is_cluster:
            cluster_ci = schedule.ci
            cluster_counts[cluster_ci] = cluster_counts.get(cluster_ci, 0) + 1

    # Calculate needs for each tenant type
    for cluster_ci, tenant_schedules in tenant_groups.items():
        tenant_count = len(tenant_schedules)
        clusters_in_csv = cluster_counts.get(cluster_ci, 0)

        # Try to get capacity from pool
        # Use first tenant schedule to query capacity
        first_tenant = tenant_schedules[0]
        capacity = check_cluster_capacity(first_tenant.ci, first_tenant.namespace)

        if capacity:
            capacity_per_cluster = capacity.max_placements_per_cluster
        else:
            # Fallback if can't query pool (assume conservative 20)
            capacity_per_cluster = 20
            logger.warning(f"Could not query capacity for {cluster_ci}, assuming {capacity_per_cluster} per cluster")

        clusters_needed = math.ceil(tenant_count / capacity_per_cluster)
        deficit = max(0, clusters_needed - clusters_in_csv)
        total_deficit += deficit

        needs.append({
            "cluster_ci": cluster_ci,
            "tenant_ci_example": first_tenant.ci,
            "tenant_count": tenant_count,
            "capacity_per_cluster": capacity_per_cluster,
            "clusters_needed": clusters_needed,
            "clusters_in_csv": clusters_in_csv,
            "deficit": deficit,
            "pool_available": capacity.available_clusters if capacity else None,
        })

    return {
        "needs": needs,
        "total_tenant_count": total_tenant_count,
        "total_deficit": total_deficit
    }


def _cluster_json(args: list[str], env=None) -> dict:
    """Read a target resource. Permission and transport errors are not absence."""
    import json
    import subprocess

    result = subprocess.run(
        ["oc", *args], capture_output=True, text=True, timeout=30, env=env,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Cluster query failed")
    return json.loads(result.stdout)


def tenant_cluster_components(catalog_item: dict) -> list[str]:
    """Babylon CatalogItem.spec.sandboxes[].tenantCluster.componentName contract."""
    return list(dict.fromkeys(
        sandbox["tenantCluster"]["componentName"]
        for sandbox in catalog_item.get("spec", {}).get("sandboxes", [])
        if sandbox.get("tenantCluster") and sandbox["tenantCluster"].get("componentName")
    ))


def uses_direct_sandbox_assignment(catalog_item: dict) -> bool:
    """Return True if the CI uses direct OcpSandbox cloud-selector assignment (no TenantClusterPool)."""
    return any(
        sandbox.get("cloudSelector") and not sandbox.get("tenantCluster")
        for sandbox in catalog_item.get("spec", {}).get("sandboxes", [])
    )


def _list_tenant_cluster_pools(env=None) -> dict[str, int]:
    data = _cluster_json(
        ["get", "tenantclusterpools", "-n", "shared-clusters", "-o", "json"], env,
    )
    return {
        pool["metadata"]["name"]: sum(
            c.get("sandboxApiState") == "available"
            for c in pool.get("status", {}).get("clusters", [])
        )
        for pool in data.get("items", [])
    }


def check_tenant_cluster_references(schedules: list[Any], *, env=None) -> dict[str, Any]:
    """Check all reference pools on the selected target, without guessing names.

    Workshop Manager clones shared reference definitions into the workshop
    namespace and provisions capacity there. Empty/disabled shared templates
    are valid for this path; they need not contain already available clusters.
    Direct ResourceClaims still need existing shared capacity.
    """
    from rhdp_flow import get_catalog_namespace, is_cluster_ci

    result = {
        "missing_refs": [], "ref_no_pool": [], "pool_no_capacity": [], "ready": [],
        "total_tenant_count": 0, "checked": True,
    }
    tenants = [s for s in schedules if s.is_tenant]
    result["total_tenant_count"] = len(tenants)
    if not tenants:
        return result
    pools = _list_tenant_cluster_pools(env)
    batch = {(s.ci, s.namespace) for s in schedules if s.is_cluster or is_cluster_ci(s.ci)}
    catalogs = {}
    for schedule in tenants:
        catalog_ns = get_catalog_namespace(schedule.ci, getattr(schedule, "catalog_namespace", "") or None)
        key = (schedule.ci, catalog_ns)
        if key not in catalogs:
            catalogs[key] = _cluster_json(
                ["get", "catalogitem", schedule.ci, "-n", catalog_ns, "-o", "json"], env,
            )
        refs = tenant_cluster_components(catalogs[key])
        detected = getattr(schedule, "detected_cluster_ci", None)
        # Catalog items that use direct OcpSandbox cloud-selector assignment have no
        # tenantCluster.componentName — sandbox-api handles cluster allocation automatically.
        # Don't warn about missing pools for these; they're always ready.
        if not refs and uses_direct_sandbox_assignment(catalogs[key]):
            result["ready"].append({
                "ci": schedule.ci, "namespace": catalog_ns,
                "target_namespace": schedule.namespace,
                "cluster_ref": "", "cluster_ci_from_csv": detected or "none",
                "workshop_name": schedule.ci_name, "pool_exists": True,
                "pool_available_clusters": -1,
                "managed_by_workshop": True,
                "has_cluster_row": False,
            })
            continue
        for ref in refs or [""]:
            exists = ref in pools
            managed = bool(schedule.enable_workshop_interface and ref)
            record = {
                "ci": schedule.ci, "namespace": catalog_ns,
                "target_namespace": schedule.namespace,
                "cluster_ref": ref, "cluster_ci_from_csv": detected or "none",
                "workshop_name": schedule.ci_name, "pool_exists": exists,
                "pool_available_clusters": pools.get(ref, 0),
                "managed_by_workshop": managed,
                "has_cluster_row": (ref or detected, schedule.namespace) in batch,
            }
            tier = (
                "missing_refs" if not ref else
                "ref_no_pool" if not exists else
                "ready" if managed or pools[ref] > 0 else "pool_no_capacity"
            )
            result[tier].append(record)
    return result
