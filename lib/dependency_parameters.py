"""Validate catalog inputs without exposing values or fetching remote schemas."""

from jsonschema import Draft7Validator
from jsonschema.exceptions import SchemaError


class ParameterValidationError(ValueError):
    pass


def _reject_references(value):
    if isinstance(value, dict):
        if any(key in value for key in ("$ref", "$dynamicRef", "$recursiveRef")):
            raise ParameterValidationError("unsupported_catalog_reference")
        for child in value.values():
            _reject_references(child)
    elif isinstance(value, list):
        for child in value:
            _reject_references(child)


def validate_parameters(schema: list[dict], values: dict, *, pending: set[str] | None = None) -> None:
    """Pending parent bindings may omit values during preview, never at submission."""
    pending = pending or set()
    definitions = {item["name"]: item for item in schema if isinstance(item, dict) and "name" in item}
    if len(definitions) != len(schema):
        raise ParameterValidationError("invalid_catalog_schema")
    if (set(values) | pending) - definitions.keys():
        raise ParameterValidationError("unknown_catalog_parameter")
    if set(values) & pending:
        raise ParameterValidationError("binding_parameter_collision")
    for name, item in definitions.items():
        spec = item.get("openAPIV3Schema") or {}
        _reject_references(spec)
        try:
            Draft7Validator.check_schema(spec)
        except SchemaError:
            raise ParameterValidationError("invalid_catalog_schema") from None
        if name in pending:
            continue
        if name not in values:
            if item.get("required") and "default" not in spec:
                raise ParameterValidationError("required_catalog_parameter_missing")
            continue
        if not Draft7Validator(spec).is_valid(values[name]):
            raise ParameterValidationError("catalog_parameter_value_invalid")


def static_parameters(catalog_parameters: dict | None, pending: set[str]) -> dict:
    schema = (catalog_parameters or {}).get("schema", [])
    values = {}
    for item in schema:
        value = item.get("value")
        if value is None or value == "":
            continue
        spec = item.get("openAPIV3Schema") or {}
        if spec.get("writeOnly") or spec.get("format") == "password" or item.get("sensitive"):
            raise ParameterValidationError("static_sensitive_parameter_forbidden")
        values[item["name"]] = value
    validate_parameters(schema, values, pending=pending)
    return values
