"""Resolve explicit parent outputs in memory. Never include values in errors."""

import math

from lib.dependency_contract import DependencyBinding


class OutputBindingError(Exception):
    def __init__(self, code: str, *, retryable: bool = False):
        self.code = code
        self.retryable = retryable
        super().__init__(code)


def resolve_parent_outputs(
    claim: dict, expected_uid: str, bindings: list[DependencyBinding],
) -> dict:
    """Extract top-level provision_data keys from uniquely named claim resources.

    The caller must separately establish successful provisioning before calling;
    credentials appearing early are not evidence of parent readiness.
    """
    if not isinstance(claim, dict):
        raise OutputBindingError("parent_resource_invalid")
    metadata = claim.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise OutputBindingError("parent_identity_mismatch")
    if not expected_uid or metadata.get("uid") != expected_uid:
        raise OutputBindingError("parent_identity_mismatch")
    if metadata.get("deletionTimestamp"):
        raise OutputBindingError("parent_deleted")
    status = claim.get("status") or {}
    if not isinstance(status, dict):
        raise OutputBindingError("parent_resource_invalid")
    resources = status.get("resources") or []
    if not isinstance(resources, list):
        raise OutputBindingError("parent_resource_invalid")
    values = {}
    for binding in bindings:
        matches = [r for r in resources if isinstance(r, dict) and r.get("name") == binding.resource_name]
        if len(matches) > 1:
            raise OutputBindingError("parent_resource_ambiguous")
        if not matches:
            raise OutputBindingError("parent_resource_missing", retryable=True)
        state = matches[0].get("state") or {}
        if not isinstance(state, dict) or state.get("kind") != "AnarchySubject":
            raise OutputBindingError("parent_resource_invalid")
        spec = state.get("spec") or {}
        if not isinstance(spec, dict):
            raise OutputBindingError("parent_resource_invalid")
        variables = spec.get("vars") or {}
        if not isinstance(variables, dict):
            raise OutputBindingError("parent_resource_invalid")
        data = variables.get("provision_data") or {}
        if not isinstance(data, dict):
            raise OutputBindingError("parent_resource_invalid")
        if binding.source_key not in data or data[binding.source_key] is None or data[binding.source_key] == "":
            raise OutputBindingError("parent_output_missing", retryable=True)
        value = data[binding.source_key]
        if type(value) not in (str, int, float, bool):
            raise OutputBindingError("parent_output_type_invalid")
        if isinstance(value, float) and not math.isfinite(value):
            raise OutputBindingError("parent_output_type_invalid")
        if binding.target_parameter in values:
            raise OutputBindingError("duplicate_binding_target")
        values[binding.target_parameter] = value
    return values
