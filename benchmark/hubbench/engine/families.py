"""Family contract: a schema, provider-shaped tools, and a task builder."""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:  # pragma: no cover
    from .world import World

CONTEXT_TOOL = "hubbench.context.get"
SUBMIT_TOOL = "hubbench.submit_answer"
ENGINE_TOOLS = (CONTEXT_TOOL, SUBMIT_TOOL)
ENGINE_SERVER_DESCRIPTION = "Benchmark-only discovery and structured answer submission controls."


@dataclass(frozen=True)
class ToolSpec:
    """One provider-shaped tool backed by the family's SQLite world."""

    name: str
    description: str
    input_schema: dict[str, Any]
    hint: str  # "read" | "write"
    handler: Callable[["World", dict[str, Any]], dict[str, Any]]
    shape: str = ""
    idempotent: bool = True

    def __post_init__(self) -> None:
        if self.hint not in {"read", "write"}:
            raise ValueError(f"{self.name}: hint must be read or write")
        if "." not in self.name:
            raise ValueError(f"{self.name}: tool names are <server>.<resource>.<operation>")

    @property
    def server(self) -> str:
        return self.name.split(".", 1)[0]


@dataclass(frozen=True)
class Family:
    slug: str
    name: str
    version: str
    cluster: str
    description: str
    schema_sql: str
    servers: dict[str, str]
    tools: tuple[ToolSpec, ...]
    build_tasks: Callable[[], list[dict[str, Any]]]
    organization: dict[str, Any] = field(default_factory=dict)
    as_of: str = ""

    def __post_init__(self) -> None:
        names = [tool.name for tool in self.tools]
        if len(names) != len(set(names)):
            raise ValueError(f"{self.slug}: duplicate tool names")
        unknown_servers = sorted({tool.server for tool in self.tools} - set(self.servers))
        if unknown_servers:
            raise ValueError(f"{self.slug}: tools mounted on undeclared servers {unknown_servers}")

    @property
    def tool_by_name(self) -> dict[str, ToolSpec]:
        return {tool.name: tool for tool in self.tools}

    @property
    def read_tools(self) -> frozenset[str]:
        return frozenset({tool.name for tool in self.tools if tool.hint == "read"} | {CONTEXT_TOOL})

    @property
    def write_tools(self) -> frozenset[str]:
        return frozenset({tool.name for tool in self.tools if tool.hint == "write"} | {SUBMIT_TOOL})

    def server_contracts(self) -> dict[str, dict[str, Any]]:
        contracts = {
            server: {"description": description, "tools": sorted(tool.name for tool in self.tools if tool.server == server)}
            for server, description in self.servers.items()
        }
        contracts["hubbench"] = {"description": ENGINE_SERVER_DESCRIPTION, "tools": list(ENGINE_TOOLS)}
        return contracts


def public_tool_definitions(family: Family, answer_schema: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """MCP ``tools/list`` entries with read/write hints; the verifier is never listed."""

    definitions: list[dict[str, Any]] = []
    for tool in family.tools:
        definitions.append(
            {
                "name": tool.name,
                "description": tool.description,
                "inputSchema": tool.input_schema,
                "annotations": {
                    "title": tool.name,
                    "readOnlyHint": tool.hint == "read",
                    "destructiveHint": tool.hint == "write",
                    "idempotentHint": tool.idempotent,
                    "openWorldHint": False,
                },
                "_meta": {"hubbench": {"server": tool.server, "hint": tool.hint, "shape": tool.shape}},
            }
        )
    definitions.append(
        {
            "name": CONTEXT_TOOL,
            "description": "Return the isolated task scope: case reference, immutable record handles, mounted servers, identity, and the evidence index.",
            "inputSchema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
            "annotations": {"title": CONTEXT_TOOL, "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
            "_meta": {"hubbench": {"server": "hubbench", "hint": "read", "shape": "benchmark control"}},
        }
    )
    definitions.append(
        {
            "name": SUBMIT_TOOL,
            "description": "Record the structured decision for the current task. Every field of the task's answer contract is required; resubmission replaces earlier values.",
            "inputSchema": answer_schema or {"type": "object", "properties": {}, "required": [], "additionalProperties": True},
            "annotations": {"title": SUBMIT_TOOL, "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
            "_meta": {"hubbench": {"server": "hubbench", "hint": "write", "shape": "benchmark control"}},
        }
    )
    return definitions


def load_family(slug: str) -> Family:
    module = importlib.import_module(f"hubbench.families.{slug}")
    family = getattr(module, "FAMILY", None)
    if not isinstance(family, Family):
        raise ValueError(f"hubbench.families.{slug} does not export FAMILY")
    return family


__all__ = [
    "CONTEXT_TOOL",
    "ENGINE_TOOLS",
    "Family",
    "SUBMIT_TOOL",
    "ToolSpec",
    "load_family",
    "public_tool_definitions",
]
