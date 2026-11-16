"""Validate num_users parameter support in CSV sessions."""


def validate_num_users_in_sessions(sessions: list[dict]) -> list[dict]:
    """Check if sessions with users override are attempting to override labs without num_users support.

    Sessions: list of dicts with at minimum 'CI Name' and 'Users' keys from CSV.
    Returns list of violations with CI name, row number, and users override value.

    This function checks the actual catalog to determine num_users support per lab.
    Labs without num_users parameter in catalog spec cannot use users_override.
    """
    violations = []

    # Lazy import to avoid circular dependencies and allow Flow to work standalone
    try:
        from rhdp_flow import catalog_items_by_fqcn
        catalog = catalog_items_by_fqcn()
    except (ImportError, RuntimeError):
        # Catalog unavailable — skip validation (will be caught by labagator validation)
        return []

    for row_num, session in enumerate(sessions, start=2):
        ci_name = (session.get("CI Name") or "").strip()
        users = session.get("Users", "")

        # Only check if users override is set and non-empty
        if not users or not str(users).strip():
            continue

        # Check if this CI exists in catalog and supports num_users
        if ci_name and ci_name in catalog:
            item = catalog[ci_name]
            # Check if 'users' or 'num_users' parameter exists in the spec
            params = item.spec.openAPIV3Schema.get("properties", {})
            if "users" not in params and "num_users" not in params:
                violations.append({
                    "ci_name": ci_name,
                    "row_number": row_num,
                    "users_override": users,
                    "error": "Lab does not support num_users parameter. Cannot override.",
                })

    return violations
