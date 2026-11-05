"""Multi-cluster deploy target resolution.

rhdp-flow can deploy workshops to physical OpenShift clusters other than the one
it runs on. Each target cluster is described by a Secret in the app's own
namespace named ``cluster-<key>`` and labelled with ``CLUSTER_LABEL``, holding:

    server        API server URL (e.g. https://api.ocp-...:6443)
    token         long-lived ServiceAccount token for that cluster
    ca.crt        (optional) PEM CA bundle for the API server
    display-name  (optional) human-friendly label for the UI

These Secrets are populated by the External Secrets Operator from AWS Secrets
Manager (Feature 2, Option B), but nothing here depends on ESO — a manually
created Secret of the same shape works identically.

Given a target key, :func:`resolve_kubeconfig` writes a short-lived kubeconfig
file (mode 0600) that the existing ``oc`` subprocess machinery consumes via
``config.kubeconfig_path``. Callers are responsible for deleting the returned
path when the deploy finishes (see ``cleanup_kubeconfig``).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import tempfile
import threading
import time

logger = logging.getLogger(__name__)

# Label that marks a Secret as a deploy-target cluster descriptor.
CLUSTER_LABEL = "rhdp-flow.redhat.com/cluster-target=true"

# Namespace the app runs in (and where target-cluster Secrets live).
_NS = (
    os.environ.get("POD_NAMESPACE")
    or os.environ.get("NAMESPACE")
    or "rhdp-flow"
)


def _oc(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["oc", *args], capture_output=True, text=True, timeout=30)


def _b64(value: str | None) -> str | None:
    if not value:
        return None
    return base64.b64decode(value).decode("utf-8")


def list_target_clusters() -> list[dict[str, str]]:
    """Return ``[{"key", "display_name"}]`` for all configured target clusters.

    Returns an empty list (never raises) if none are configured or the lookup
    fails, so the UI degrades gracefully to "in-cluster only".
    """
    result = _oc(["get", "secret", "-n", _NS, "-l", CLUSTER_LABEL, "-o", "json"])
    if result.returncode != 0:
        logger.warning("Could not list target clusters: %s", result.stderr.strip())
        return []
    try:
        items = json.loads(result.stdout or "{}").get("items", [])
    except json.JSONDecodeError:
        logger.warning("Could not parse target cluster list output")
        return []

    clusters: list[dict[str, str]] = []
    for item in items:
        name = item.get("metadata", {}).get("name", "")
        key = name.removeprefix("cluster-")
        if not key:
            continue
        data = item.get("data", {})
        display = _b64(data.get("display-name")) or key
        clusters.append({"key": key, "display_name": display})
    return sorted(clusters, key=lambda c: c["key"])


# How long a "does cluster-<key> exist?" answer is trusted before re-checking.
_CONFIGURED_TTL_SECONDS = 60.0
_configured_cache: dict[str, tuple[float, bool]] = {}
_configured_lock = threading.Lock()


def is_configured(target_cluster: str | None) -> bool:
    """True if a ``cluster-<target>`` Secret exists in the app namespace.

    Cached for ``_CONFIGURED_TTL_SECONDS`` so it is cheap to call per request.
    Never raises: a missing ``oc`` binary (CI, local dev without a cluster),
    a timeout, or a lookup error all mean "not configured", so callers fall
    back to the in-cluster / KUBECONFIG credentials exactly as before.
    """
    if not target_cluster:
        return False
    now = time.monotonic()
    with _configured_lock:
        hit = _configured_cache.get(target_cluster)
        if hit and now - hit[0] < _CONFIGURED_TTL_SECONDS:
            return hit[1]
    try:
        ok = _oc(["get", "secret", f"cluster-{target_cluster}", "-n", _NS, "-o", "name"]).returncode == 0
    except (OSError, subprocess.SubprocessError):
        ok = False
    with _configured_lock:
        _configured_cache[target_cluster] = (now, ok)
    return ok


def _reset_configured_cache() -> None:
    """Test hook: forget cached ``is_configured`` answers."""
    with _configured_lock:
        _configured_cache.clear()


def _build_kubeconfig(server: str, token: str, ca: str | None) -> str:
    cluster_block: dict[str, object] = {"server": server}
    if ca:
        cluster_block["certificate-authority-data"] = base64.b64encode(
            ca.encode("utf-8")
        ).decode("ascii")
    else:
        # No CA supplied: fall back to skipping TLS verification. Prefer always
        # supplying ca.crt in the Secret for production targets.
        cluster_block["insecure-skip-tls-verify"] = True
        logger.warning("Target cluster kubeconfig built without a CA bundle")

    kubeconfig = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{"name": "target", "cluster": cluster_block}],
        "users": [{"name": "target", "user": {"token": token}}],
        "contexts": [
            {"name": "target", "context": {"cluster": "target", "user": "target"}}
        ],
        "current-context": "target",
    }
    return json.dumps(kubeconfig)


def resolve_kubeconfig(target_cluster: str | None) -> str | None:
    """Build an ephemeral kubeconfig for ``target_cluster``.

    Returns the path to a temp kubeconfig file, or ``None`` when no target is
    given (meaning: deploy to the in-cluster ServiceAccount as before).

    Raises :class:`ValueError` for an unknown or malformed target.
    """
    if not target_cluster:
        return None

    name = f"cluster-{target_cluster}"
    result = _oc(["get", "secret", name, "-n", _NS, "-o", "json"])
    if result.returncode != 0:
        raise ValueError(f"Unknown deploy target cluster '{target_cluster}'")

    data = json.loads(result.stdout).get("data", {})
    server = _b64(data.get("server"))
    token = _b64(data.get("token"))
    ca = _b64(data.get("ca.crt"))
    if not server or not token:
        raise ValueError(
            f"Target cluster secret '{name}' is missing 'server' or 'token'"
        )

    content = _build_kubeconfig(server, token, ca)
    fd, path = tempfile.mkstemp(prefix=f"kubeconfig-{target_cluster}-", suffix=".yaml")
    try:
        os.write(fd, content.encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(path, 0o600)
    return path


def resolve_and_cleanup_check(target_cluster: str | None) -> None:
    """Validate a target cluster key without leaving a temp file behind.

    Raises :class:`ValueError` if the target is unknown or malformed; a no-op
    when ``target_cluster`` is falsy.
    """
    path = resolve_kubeconfig(target_cluster)
    cleanup_kubeconfig(path)


def cleanup_kubeconfig(path: str | None) -> None:
    """Delete a kubeconfig produced by :func:`resolve_kubeconfig`."""
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        logger.debug("Could not remove temp kubeconfig %s", path)
