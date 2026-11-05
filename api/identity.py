"""Identity-based access control for the multi-cluster deploy picker.

Choosing a *deploy-target cluster* (Feature 2) is available to any user who
reaches the app through the OAuth proxy (``X-Forwarded-Email`` /
``X-Forwarded-User``). Who can reach the app at all is controlled by the
oauth-proxy email list (``authenticated-emails.txt``) — keep that in sync with
Labagator operators (Josh, Billy, Patrick, …).

``DEPLOY_PICKER_ALLOWED_EMAILS`` is optional: when set, only those emails may
**leave** the default target (Events). When unset or ``*``, any authenticated
user may pick any configured cluster. The default target ``events`` never
requires special access so Labagator embeds still land on us-west-2.
"""

from __future__ import annotations

import logging
import os

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)

# Default target — always allowed without an operator allowlist check.
DEFAULT_TARGET_CLUSTER = os.environ.get("RHDP_DEFAULT_TARGET_CLUSTER", "events").strip() or "events"

# Optional tighter gate for non-default targets. Unused when env is ``*`` (default).
_OPERATOR_EMAILS = "jdisrael@redhat.com,bbethell@redhat.com,prutledg@redhat.com"


def effective_target(requested: str | None) -> str | None:
    """Resolve the cluster a request should run against.

    An explicit selection always wins. With no selection, use the default
    target (Events / us-west-2) whenever its ``cluster-<key>`` Secret exists —
    Flow's own cluster (infra01) has no Babylon catalog namespaces, so falling
    back to it silently breaks catalog lookups, validation, deploy and QA.
    If the default target is not configured (CI, local dev), return ``None`` so
    callers keep using the in-cluster / KUBECONFIG credentials.
    """
    requested = (requested or "").strip()
    if requested:
        return requested
    from api import cluster_targets  # local import: keep identity import-light

    if cluster_targets.is_configured(DEFAULT_TARGET_CLUSTER):
        return DEFAULT_TARGET_CLUSTER
    return None


def _allowed_emails() -> set[str] | None:
    """Return the non-default-target allowlist, or None if any authenticated user may pick."""
    raw = os.environ.get("DEPLOY_PICKER_ALLOWED_EMAILS", "*").strip()
    if not raw or raw == "*":
        return None
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def get_user_email(request: Request) -> str | None:
    """Return the authenticated user's email from the OAuth proxy headers."""
    email = request.headers.get("X-Forwarded-Email") or request.headers.get(
        "X-Forwarded-User"
    )
    return email.strip().lower() if email else None


def is_picker_allowed(request: Request) -> bool:
    """True if the requesting user may change deploy-target away from the default.

    Labagator embeds often authenticate with the shared API key only (no
    ``X-Forwarded-Email``). Those sessions still see the picker and default to
    Events; leaving Events for infra01/integration/prod is allowed whenever
    ``DEPLOY_PICKER_ALLOWED_EMAILS=*`` (the default). When a tighter email list
    is configured, an OAuth email on that list is required to leave Events.
    """
    allowed = _allowed_emails()
    if allowed is None:
        # Wide open: any caller that passed API-key auth may use the picker.
        return True
    email = get_user_email(request)
    return bool(email and email in allowed)

def require_picker_access(request: Request, target_cluster: str | None = None) -> None:
    """Raise HTTP 403 unless the user may deploy to ``target_cluster``.

    The default target (Events / us-west-2) and empty (legacy in-cluster) do not
    require picker access. Other targets require an authenticated, allowlisted
    (or any-auth when allowlist is ``*``) user.
    """
    if not target_cluster or target_cluster == DEFAULT_TARGET_CLUSTER:
        return
    if not is_picker_allowed(request):
        email = get_user_email(request) or "unauthenticated"
        logger.warning("Denied deploy-target selection for user %s → %s", email, target_cluster)
        raise HTTPException(
            403,
            "Choosing a non-default deploy-target cluster requires an authenticated operator.",
        )
