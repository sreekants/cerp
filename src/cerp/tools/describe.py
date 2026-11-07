"""Generic ``<module>.describe_<model>`` tools (MPC001-080, MPC001-082, MPC001-087).

A describe tool returns the exposed fields of one model, their types and descriptions, the sortable fields,
the verbs offered and the handle format. It reads only the registry and exposure, never business data, so
it needs no record-level authorization.
"""

from __future__ import annotations

from collections.abc import Callable

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations
from pydantic import BaseModel

from cerp.data.exposure import ExposedModel, Exposure
from cerp.protocol.killswitch import NO_SWITCHES, KillSwitch
from cerp.protocol.naming import generic_name
from cerp.protocol.scopes import NO_SCOPES, ScopePolicy
from cerp.protocol.tooling import register_tool

READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)


class FieldInfo(BaseModel):
    name: str
    type: str
    required: bool
    description: str | None = None


class ModelDescription(BaseModel):
    module: str
    model: str
    description: str
    handle_format: str
    verbs: list[str]
    sortable: list[str]
    fields: list[FieldInfo]


def describe(em: ExposedModel) -> ModelDescription:
    cols = {c.name: c for c in em.model.columns or ()}
    return ModelDescription(
        module=em.module,
        model=em.name,
        description=em.description,
        handle_format=f"{em.module}:{em.name}:<id>",
        verbs=list(em.verbs),
        sortable=list(em.sortable),
        fields=[
            FieldInfo(
                name=f,
                type=(cols[f].type or "TEXT").upper(),
                required=cols[f].not_null and cols[f].default is None and not cols[f].primary_key,
                description=em.field_descriptions.get(f),
            )
            for f in em.fields
        ],
    )


def _bind(result: ModelDescription, doc: str) -> Callable[[], ModelDescription]:
    def describe_model() -> ModelDescription:
        return result

    describe_model.__doc__ = doc
    return describe_model


def describe_tool_names(exposure: Exposure) -> list[str]:
    """Names of the describe tools an exposure produces, in registration order."""
    return [generic_name(em.module, "describe", em.name) for em in exposure.models if "describe" in em.verbs]


def register_describe_tools(
    mcp: MCPServer, exposure: Exposure, killswitch: KillSwitch = NO_SWITCHES, scopes: ScopePolicy = NO_SCOPES
) -> list[str]:
    """Register one describe tool per exposed model that offers the ``describe`` verb."""
    names: list[str] = []
    for em in exposure.models:
        if "describe" not in em.verbs:
            continue
        name = generic_name(em.module, "describe", em.name)
        doc = f"Describe the fields of {em.module}:{em.name} records: {em.description}"
        register_tool(
            mcp,
            _bind(describe(em), doc),
            name=name,
            title=f"Describe {em.name}",
            annotations=READ_ONLY,
            killswitch=killswitch,
            scopes=scopes,
        )
        names.append(name)
    return names
