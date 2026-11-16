"""Validate num_users parameter support in CSV sessions."""


LABS_WITHOUT_NUM_USERS = {
    "lb1048-demyst-aap-aws-tenant", "lb1063-defend-contain-comply",
    "lb1066-rh1-ocp5-opp", "lb1082-adv-network-automation-workshop",
    "lb1141-redhat-lightspeed", "lb1284-intro-ao-cnv",
    "lb1285-ai-agent-sec-aws-tenant", "lb1353-konflux-tsf-tssc-aws-tenant",
    "lb1543-ocp-ops-day-cnv", "lb1657-sovereign-cloud-osac-cnv",
    "lb1663-rh1-ans-island-leaderboard-cnv", "lb1803-aro-hcp-sandbox-azure",
    "lb1815-lightwell-tenant", "lb1845-redhat-ai-inference-aws",
    "lb1858-ocp5-container-mngmt-tenant", "lb1898-ocp-virt-troubleshooting-tenant",
    "lb1932-ocp-advanced-platform-aws", "lb2047a-rhel-image-mode",
    "lb2047b-rhel-image-mode", "lb2047c-rhel-image-mode", "lb2047d-rhel-image-mode",
    "lb2062a-rhel-zero-cve", "lb2062b-rhel-zero-cve", "lb2075a-sat-compliance",
    "lb2075c-sat-lightspeed", "lb2075d-sat-lightspeed-ansible",
    "lb2081a-post-quantum-crypto", "lb2081b-post-quantum-crypto",
    "lb2081c-post-quantum-crypto", "lb2111-ocp-dev-day-cnv-tenant",
    "lb2142-ai-quickstarts-tenant", "lb8000-building-ai-tenant"
}


def validate_num_users_in_sessions(sessions: list[dict]) -> list[dict]:
    """Check if sessions with users override are attempting to override locked labs.

    Sessions: list of dicts with at minimum 'CI Name' and 'Users' keys from CSV.
    Returns list of violations with CI name, row number, and users override value.
    """
    violations = []
    for row_num, session in enumerate(sessions, start=2):
        ci_name = (session.get("CI Name") or "").lower().strip()
        users = session.get("Users", "")

        # Only check if users override is set and non-empty
        if not users or not str(users).strip():
            continue

        # Check if this CI is in the locked-instances list
        if any(locked in ci_name for locked in LABS_WITHOUT_NUM_USERS):
            violations.append({
                "ci_name": session.get("CI Name", ""),
                "row_number": row_num,
                "users_override": users,
                "error": f"Lab does not support num_users parameter. Instances are locked at 5.",
            })

    return violations
