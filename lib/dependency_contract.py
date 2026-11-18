"""Versioned, credential-free Parent–Child configuration and execution contract."""

import re
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$", max_length=128)]
ResourceName = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9.-]*$", max_length=253)]
Scope = Literal["session", "shared_group"]
Role = Literal["parent", "child"]
_CREDENTIAL_NAME = re.compile(r"password|passwd|secret|token|credential|private.?key", re.I)


class DependencyModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, allow_inf_nan=False)


class DependencyBinding(DependencyModel):
    resource_name: ResourceName
    source_key: Identifier
    target_parameter: Identifier
    sensitive: bool = True

    @model_validator(mode="after")
    def protect_credential_sensitivity(self) -> Self:
        if _CREDENTIAL_NAME.search(self.source_key) or _CREDENTIAL_NAME.search(self.target_parameter):
            self.sensitive = True
        return self


def _unique_targets(bindings: list[DependencyBinding]) -> None:
    targets = [binding.target_parameter for binding in bindings]
    if len(targets) != len(set(targets)):
        raise ValueError("duplicate_binding_target")


class DependencyMember(DependencyModel):
    lab_id: int = Field(gt=0)
    role: Role
    quantity_policy: Literal["fixed", "session_seats"] = "fixed"
    fixed_count: int = Field(default=1, ge=1, le=10000)
    bindings: list[DependencyBinding] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_member(self) -> Self:
        if self.role == "parent" and (
            self.quantity_policy != "fixed" or self.fixed_count != 1 or self.bindings
        ):
            raise ValueError("parent_requires_one_instance_without_bindings")
        _unique_targets(self.bindings)
        return self


class DependencyDefinitionInput(DependencyModel):
    scope: Scope = "session"
    parent_lead_minutes: int = Field(default=240, ge=0, le=43200)
    readiness_timeout_minutes: int = Field(default=240, ge=1, le=43200)
    members: list[DependencyMember] = Field(min_length=2, max_length=100)

    @model_validator(mode="after")
    def validate_members(self) -> Self:
        if sum(member.role == "parent" for member in self.members) != 1:
            raise ValueError("exactly_one_parent_required")
        ids = [member.lab_id for member in self.members]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate_member")
        return self


class DependencyNode(DependencyModel):
    key: ResourceName
    role: Role
    lab_id: int = Field(gt=0)
    ci_name: ResourceName
    catalog_namespace: ResourceName
    count: int = Field(ge=1, le=10000)
    users: int = Field(ge=1, le=10000)
    # Catalog validation must additionally reject parameters declared sensitive by
    # their schema. This name guard prevents obvious credentials in raw plans.
    parameters: dict[str, str | int | float | bool] = Field(default_factory=dict)
    bindings: list[DependencyBinding] = Field(default_factory=list, max_length=100)
    deploy_on: AwareDatetime
    auto_destroy: AwareDatetime
    workshop_name: ResourceName

    @model_validator(mode="after")
    def validate_node(self) -> Self:
        if self.role == "parent" and (self.count != 1 or self.bindings):
            raise ValueError("invalid_parent_node")
        if self.auto_destroy <= self.deploy_on:
            raise ValueError("invalid_node_lifetime")
        _unique_targets(self.bindings)
        if set(self.parameters) & {b.target_parameter for b in self.bindings}:
            raise ValueError("binding_parameter_collision")
        if any(_CREDENTIAL_NAME.search(k) for k in self.parameters):
            raise ValueError("static_sensitive_parameter_forbidden")
        return self


def safe_dependency_validation_errors(error: ValidationError) -> list[dict]:
    """Return HTTP-safe detail for callers catching dependency validation errors.

    Pydantic's hide_input_in_errors only protects str(error). API consumers must
    use this helper instead of returning errors() or json(), which retain input.
    Messages and unknown location keys are redacted because both can contain
    caller-provided data. Catalog dictionary keys are never public locations.
    """
    fields = set().union(*(model.model_fields for model in (
        DependencyBinding, DependencyMember, DependencyDefinitionInput,
        DependencyNode, DependencyPlan,
    )))
    detail = []
    for item in error.errors(include_input=False, include_context=False, include_url=False):
        location = []
        in_parameters = False
        for part in item["loc"]:
            if isinstance(part, int):
                location.append(part)
            elif not in_parameters and part in fields:
                location.append(part)
            else:
                location.append("<field>")
            if part == "parameters":
                in_parameters = True
        detail.append({"loc": location, "type": item["type"],
                       "msg": "Invalid dependency configuration"})
    return detail


class DependencyPlan(DependencyModel):
    version: Literal["parent_child_v1"] = "parent_child_v1"
    run_id: ResourceName
    event_id: int = Field(gt=0)
    cluster_id: int = Field(gt=0)
    namespace: ResourceName
    scope: Scope = "session"
    scope_key: str = Field(min_length=1, max_length=500)
    revision: int = Field(ge=1)
    session_ids: list[int] = Field(min_length=1, max_length=1000)
    readiness_timeout_minutes: int = Field(default=240, ge=1, le=43200)
    nodes: list[DependencyNode] = Field(min_length=2, max_length=10000)

    @model_validator(mode="after")
    def validate_plan(self) -> Self:
        parents = [node for node in self.nodes if node.role == "parent"]
        if len(parents) != 1:
            raise ValueError("exactly_one_parent_required")
        for values in ([node.key for node in self.nodes],
                       [node.workshop_name for node in self.nodes], self.session_ids):
            if len(values) != len(set(values)):
                raise ValueError("duplicate_plan_identity")
        if any(s <= 0 for s in self.session_ids):
            raise ValueError("invalid_session_id")
        if self.scope == "session" and len(self.session_ids) != 1:
            raise ValueError("session_scope_requires_one_session")
        parent = parents[0]
        if any(node.deploy_on < parent.deploy_on or node.auto_destroy > parent.auto_destroy
               for node in self.nodes):
            raise ValueError("parent_lifetime_must_cover_children")
        return self
