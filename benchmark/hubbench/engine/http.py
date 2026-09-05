"""HTTP surfaces for one HubBench task world: MCP over streamable HTTP, a REST API, and a website.

Run from ``benchmark/``::

    python3 -m hubbench.engine.http --family clinicops --task clinicops-001 \
        --db /tmp/clinicops-001.db --fresh --host 0.0.0.0 --port 8765

One process serves one task world — the same on-disk SQLite file the stdio MCP
server (``hubbench.engine.server``) and the terminal ``tool`` CLI use — so state
written on any surface is visible on every other, and the durable call trace
grades the whole session as one episode:

* **MCP over streamable HTTP** — ``POST /mcp`` (every tool) and
  ``POST /mcp/<server>`` (one mock server; ``/mcp/hubbench`` carries the two
  benchmark controls).  JSON-RPC 2.0 request or batch in, JSON out;
  notifications answer ``202`` with an empty body; ``GET /mcp`` answers ``405``
  (no SSE stream is offered).
* **REST** under ``/api/v1``:

  - ``GET /api/v1/tools`` (catalog), ``GET /api/v1/tools/<name>`` (schema),
    ``POST /api/v1/tools/<name>`` (JSON arguments → tool result);
  - ``GET /api/v1/task`` (discovery control), ``POST /api/v1/submit`` (answer
    submission control);
  - resource routes derived from the tool names.  A tool named
    ``<server>.<resource>.<operation>`` maps to
    ``GET /api/v1/<server>/<resource>`` (the ``list`` tool, else ``search``,
    query-string arguments), ``GET /api/v1/<server>/<resource>/<id>`` (the
    ``get`` tool, ``<id>`` bound to its single required argument) and
    ``GET|POST /api/v1/<server>/<resource>/<operation>`` (any operation:
    ``GET`` with query-string arguments for read tools, ``POST`` with a JSON
    body for write tools).  Two-part names ``<server>.<operation>`` use the
    server as their resource (``approvals.get`` → ``/api/v1/approvals/approvals/<id>``,
    shorthand ``/api/v1/approvals/<id>``).  Tools whose names fit neither shape
    stay reachable through ``/api/v1/tools/<name>``.  Query-string values are
    coerced through the tool's input schema (integer, number, boolean, JSON
    arrays / objects).

  Status codes: ``200`` success, ``400`` bad JSON or arguments (the world's
  own validation message), ``404`` unknown tool / server / resource / record,
  ``405`` wrong method (``Allow`` header), ``422`` a well-formed call the world
  rejected, ``503`` a declared transient fault (``retryable``).
* **Website** — server-rendered HTML with inline CSS, no scripts: ``GET /``
  (task brief and connected systems), ``/app/<server>``,
  ``/app/<server>/<resource>`` (listing), ``/app/<server>/<resource>/<id>``
  (detail), ``/app/<server>/<resource>/<operation>`` (a form generated from the
  input schema; write forms ``POST`` through the world and link to the
  readback), ``/app/task`` (discovery control), ``/app/submit`` (answer form).

Every tool call on every surface goes through ``World.call_tool`` on one
dedicated thread, so validation, write containment, readbacks, and the trace
behave exactly as over stdio MCP or the CLI.  The sealed verifier contract,
the expected answer, and the call trace are never readable here.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import signal
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .families import CONTEXT_TOOL, SUBMIT_TOOL, Family, load_family, public_tool_definitions
from .server import PROTOCOL_VERSION, handle_message
from .tasks import load_task
from .validation import validate_schema
from .world import World, seed_database

MAX_REQUEST_BYTES = 1_000_000
API_PREFIX = "/api/v1"
APP_PREFIX = "/app"
MCP_PREFIX = "/mcp"
COLLECTION_OPERATIONS = ("list", "search")
ITEM_OPERATION = "get"
LONG_TEXT_FIELDS = {"body", "description", "message", "note", "notes", "purpose", "reason", "summary", "text"}
_INTEGER_TEXT = re.compile(r"-?\d+")


# --------------------------------------------------------------------------- #
# Tool routing catalog (pure: derived from the family and the task's answer schema)
# --------------------------------------------------------------------------- #


def _id_parameter(schema: dict[str, Any]) -> str | None:
    """The argument an item route binds ``<id>`` to, or ``None`` when ambiguous."""

    properties = list(schema.get("properties", {}))
    required = list(schema.get("required", []))
    if len(required) == 1:
        return required[0]
    candidates = [name for name in properties if name == "id" or name.endswith("_id")]
    if len(candidates) == 1:
        return candidates[0]
    if len(properties) == 1:
        return properties[0]
    return None


@dataclass(frozen=True)
class ToolRoute:
    """One tool and the REST / website routes derived from its name."""

    name: str
    server: str
    resource: str
    operation: str
    hint: str
    description: str
    input_schema: dict[str, Any]
    id_parameter: str | None

    @property
    def is_read(self) -> bool:
        return self.hint == "read"

    @property
    def operation_path(self) -> str:
        return f"{API_PREFIX}/{self.server}/{self.resource}/{self.operation}"

    @property
    def web_path(self) -> str:
        return f"{APP_PREFIX}/{self.server}/{self.resource}/{self.operation}"

    def catalog_entry(self) -> dict[str, Any]:
        endpoints = {
            "call": f"POST {API_PREFIX}/tools/{self.name}",
            "operation": f"{'GET' if self.is_read else 'POST'} {self.operation_path}",
        }
        if self.operation in COLLECTION_OPERATIONS:
            endpoints["collection"] = f"GET {API_PREFIX}/{self.server}/{self.resource}"
        if self.operation == ITEM_OPERATION and self.id_parameter:
            endpoints["item"] = f"GET {API_PREFIX}/{self.server}/{self.resource}/<{self.id_parameter}>"
        return {
            "name": self.name,
            "server": self.server,
            "resource": self.resource,
            "operation": self.operation,
            "hint": self.hint,
            "description": self.description,
            "input_schema": self.input_schema,
            "endpoints": endpoints,
            "web": self.web_path,
        }


class Catalog:
    """Routes every public tool of one task world by ``<server>/<resource>/<operation>``."""

    def __init__(self, family: Family, task: dict[str, Any]):
        self.family = family
        self.task = task
        self.tools: dict[str, ToolRoute] = {}
        self.servers: dict[str, dict[str, dict[str, ToolRoute]]] = {}
        contracts = family.server_contracts()
        self.descriptions = {server: contract["description"] for server, contract in contracts.items()}
        for definition in public_tool_definitions(family, task["answer_schema"]):
            parts = definition["name"].split(".")
            server, operation = parts[0], parts[-1]
            resource = ".".join(parts[1:-1]) or server
            hint = definition["_meta"]["hubbench"]["hint"]
            route = ToolRoute(
                name=definition["name"],
                server=server,
                resource=resource,
                operation=operation,
                hint=hint,
                description=definition["description"],
                input_schema=definition["inputSchema"],
                id_parameter=_id_parameter(definition["inputSchema"]) if hint == "read" else None,
            )
            self.tools[route.name] = route
            self.servers.setdefault(server, {}).setdefault(resource, {})[operation] = route
        systems = [system for system in task.get("world", {}).get("systems", []) if system in self.servers]
        self.server_order = systems + [server for server in self.servers if server not in systems]

    def resources(self, server: str) -> dict[str, dict[str, ToolRoute]]:
        return self.servers.get(server, {})

    def collection(self, server: str, resource: str) -> ToolRoute | None:
        operations = self.resources(server).get(resource, {})
        for operation in COLLECTION_OPERATIONS:
            if operation in operations:
                return operations[operation]
        return None

    def item(self, server: str, resource: str) -> ToolRoute | None:
        route = self.resources(server).get(resource, {}).get(ITEM_OPERATION)
        return route if route is not None and route.id_parameter else None

    def item_shorthand(self, server: str, segment: str) -> tuple[ToolRoute, str] | None:
        """``/<server>/<id>`` for two-part tools whose resource is the server itself."""

        if segment in self.resources(server):
            return None
        route = self.item(server, server)
        return (route, segment) if route is not None else None

    def server_summary(self, server: str) -> dict[str, Any]:
        resources: dict[str, Any] = {}
        for resource, operations in self.resources(server).items():
            collection = self.collection(server, resource)
            item = self.item(server, resource)
            resources[resource] = {
                "collection": f"GET {API_PREFIX}/{server}/{resource}" if collection else None,
                "item": f"GET {API_PREFIX}/{server}/{resource}/<{item.id_parameter}>" if item else None,
                "operations": {
                    operation: {"tool": route.name, "hint": route.hint, "method": "GET" if route.is_read else "POST", "path": route.operation_path}
                    for operation, route in operations.items()
                },
            }
        return {"server": server, "description": self.descriptions.get(server, ""), "web": f"{APP_PREFIX}/{server}", "resources": resources}


# --------------------------------------------------------------------------- #
# Argument coercion for query strings and HTML forms
# --------------------------------------------------------------------------- #


def _coerce_scalar(value: Any, spec: dict[str, Any]) -> Any:
    if not isinstance(value, str):
        return value
    kind = spec.get("type")
    if isinstance(kind, list):
        kind = next((candidate for candidate in kind if candidate != "null"), None)
    text = value.strip()
    if kind == "integer":
        try:
            return int(text)
        except ValueError:
            return value
    if kind == "number":
        try:
            return int(text) if _INTEGER_TEXT.fullmatch(text) else float(text)
        except ValueError:
            return value
    if kind == "boolean":
        lowered = text.lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
        return value
    if kind == "object":
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return value
    return value


def coerce_arguments(values: Mapping[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """Turn query-string / form values (strings, possibly repeated) into typed tool arguments.

    Blank fields are omitted; unknown names pass through untouched so the
    world's own validation message reaches the caller.
    """

    properties = schema.get("properties", {})
    arguments: dict[str, Any] = {}
    for key, raw in values.items():
        items = list(raw) if isinstance(raw, (list, tuple)) else [raw]
        spec = properties.get(key, {})
        if spec.get("type") == "array":
            if len(items) == 1 and isinstance(items[0], str) and items[0].strip().startswith("["):
                try:
                    arguments[key] = json.loads(items[0])
                    continue
                except json.JSONDecodeError:
                    pass
            arguments[key] = [_coerce_scalar(item, spec.get("items", {})) for item in items if item != ""]
            continue
        value = items[-1] if items else ""
        if isinstance(value, str) and value == "":
            continue
        arguments[key] = _coerce_scalar(value, spec)
    return arguments


# --------------------------------------------------------------------------- #
# One world, one thread
# --------------------------------------------------------------------------- #


class WorldSession:
    """Owns the ``World`` on a single dedicated thread; every access is serialized through it.

    SQLite connections are never shared across threads: the HTTP handler
    threads submit closures to this one-worker executor and wait.
    """

    def __init__(self, family: Family, task: dict[str, Any], database: str | Path, *, fresh: bool = False):
        self.family = family
        self.task = task
        self.database = Path(database)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hubbench-world")
        self._world: World | None = None
        self._executor.submit(self._open, fresh).result()

    def _open(self, fresh: bool) -> None:
        if fresh or not self.database.exists():
            seed_database(self.family, self.task, self.database)
        self._world = World(self.family, self.task, self.database)

    def run(self, function: Callable[..., Any], *args: Any) -> Any:
        return self._executor.submit(function, self._world, *args).result()

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.run(World.call_tool, name, arguments)

    def rpc(self, payload: Any, server: str | None = None) -> Any:
        return self.run(handle_message, payload, server)

    def close(self) -> None:
        def _close(world: World | None) -> None:
            if world is not None:
                world.close()

        self.run(_close)
        self._executor.shutdown(wait=True)


# --------------------------------------------------------------------------- #
# HTML rendering (no scripts, inline CSS, readable from curl / lynx)
# --------------------------------------------------------------------------- #

_CSS = (
    "body{font-family:system-ui,-apple-system,'Segoe UI',Helvetica,Arial,sans-serif;margin:0;padding:1.25rem 2rem 2rem;"
    "color:#1b1f23;background:#fff;max-width:76rem;line-height:1.45}h1,h2,h3{line-height:1.2}nav a{margin-right:.9rem}"
    "table{border-collapse:collapse;margin:.5rem 0;font-size:.92rem}th,td{border:1px solid #d0d7de;padding:.3rem .55rem;"
    "text-align:left;vertical-align:top}th{background:#f6f8fa}dl{display:grid;grid-template-columns:max-content 1fr;"
    "gap:.2rem 1rem;margin:.5rem 0}dt{font-weight:600}dd{margin:0}pre{background:#f6f8fa;padding:.75rem;overflow:auto;"
    "border:1px solid #d0d7de;white-space:pre-wrap}code{background:#f6f8fa;padding:0 .2rem}form label{display:block;"
    "margin:.7rem 0 .15rem;font-weight:600}form input[type=text],form input[type=number],form select,form textarea{"
    "width:100%;max-width:42rem;padding:.35rem;font:inherit;box-sizing:border-box}form textarea{min-height:5rem}"
    "button{margin-top:1rem;padding:.45rem 1rem;font:inherit}.error{color:#b42318;border:1px solid #f04438;"
    "background:#fef3f2;padding:.6rem;margin:.6rem 0}.ok{color:#067647;border:1px solid #17b26a;background:#ecfdf3;"
    "padding:.6rem;margin:.6rem 0}.hint{color:#57606a;font-size:.9rem}.read{color:#0550ae}.write{color:#953800}"
    ".crumbs{font-size:.9rem;color:#57606a}.req{color:#b42318}footer{margin-top:2rem;border-top:1px solid #d0d7de;padding-top:.6rem}"
)


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _compact(value: Any, limit: int = 200) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _json_pre(value: Any) -> str:
    return f"<pre>{_esc(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False))}</pre>"


def _unwrap_rows(rows: list[Any]) -> list[Any]:
    """FHIR-style ``entry: [{resource: {...}}]`` lists render as the resources themselves."""

    if rows and all(isinstance(row, dict) and set(row) == {"resource"} and isinstance(row["resource"], dict) for row in rows):
        return [row["resource"] for row in rows]
    return rows


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return f"<code>{_esc(_compact(value))}</code>"
    return _esc(value)


def render_table(rows: list[dict[str, Any]], link_for: Callable[[dict[str, Any]], str | None] | None = None) -> str:
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    head = "".join(f"<th>{_esc(column)}</th>" for column in columns)
    body = []
    for row in rows:
        href = link_for(row) if link_for else None
        cells = []
        for index, column in enumerate(columns):
            cell = _cell(row.get(column))
            if index == 0 and href and cell:
                cell = f'<a href="{_esc(href)}">{cell}</a>'
            cells.append(f"<td>{cell}</td>")
        body.append(f"<tr>{''.join(cells)}</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def render_value(value: Any, *, link_for: Callable[[dict[str, Any]], str | None] | None = None, depth: int = 0) -> str:
    if isinstance(value, list):
        rows = _unwrap_rows(value)
        if not rows:
            return "<em>none</em>"
        if all(isinstance(row, dict) for row in rows):
            return render_table(rows, link_for)
        return "<ul>" + "".join(f"<li>{render_value(item, depth=depth + 1)}</li>" for item in rows) + "</ul>"
    if isinstance(value, dict):
        if not value:
            return "<em>empty</em>"
        if depth >= 2:
            return f"<code>{_esc(_compact(value, 400))}</code>"
        items = "".join(f"<dt>{_esc(key)}</dt><dd>{render_value(item, link_for=link_for, depth=depth + 1)}</dd>" for key, item in value.items())
        return f"<dl>{items}</dl>"
    if value is None:
        return "<em>null</em>"
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    if "\n" in text:
        return f"<pre>{_esc(text)}</pre>"
    return _esc(text)


def render_field(name: str, spec: dict[str, Any], required: bool, value: Any) -> str:
    label = f'<label for="f-{_esc(name)}">{_esc(name)}{" <span class=req>*</span>" if required else ""}</label>'
    description = spec.get("description")
    hint = f'<div class="hint">{_esc(description)}</div>' if description else ""
    kind = spec.get("type")
    if isinstance(kind, list):
        kind = next((candidate for candidate in kind if candidate != "null"), None)
    if isinstance(value, (dict, list)):
        text = json.dumps(value, sort_keys=True)
    elif isinstance(value, bool):
        text = "true" if value else "false"
    else:
        text = "" if value is None else str(value)
    attrs = f'id="f-{_esc(name)}" name="{_esc(name)}"'
    if "enum" in spec:
        options = "" if required else '<option value="">—</option>'
        for option in spec["enum"]:
            selected = " selected" if text == str(option) else ""
            options += f'<option value="{_esc(option)}"{selected}>{_esc(option)}</option>'
        control = f"<select {attrs}>{options}</select>"
    elif kind == "boolean":
        options = "".join(
            f'<option value="{choice}"{" selected" if text == choice else ""}>{choice or "—"}</option>' for choice in ("", "true", "false")
        )
        control = f"<select {attrs}>{options}</select>"
    elif kind in {"integer", "number"}:
        step = "1" if kind == "integer" else str(spec.get("multipleOf", "any"))
        bounds = "".join(f' {bound}="{_esc(spec[bound])}"' for bound in ("minimum", "maximum") if bound in spec)
        control = f'<input type="number" step="{step}"{bounds} {attrs} value="{_esc(text)}">'
    elif kind in {"array", "object"} or name in LONG_TEXT_FIELDS:
        placeholder = f' placeholder="JSON {kind}"' if kind in {"array", "object"} else ""
        control = f"<textarea {attrs}{placeholder}>{_esc(text)}</textarea>"
    else:
        control = f'<input type="text" {attrs} value="{_esc(text)}">'
    return label + hint + control


def render_form(schema: dict[str, Any], action: str, method: str, values: Mapping[str, Any], submit_label: str) -> str:
    properties = schema.get("properties", {})
    required = set(schema.get("required", []))
    if properties:
        fields = "".join(render_field(name, spec, name in required, values.get(name)) for name, spec in properties.items())
    else:
        fields = '<p class="hint">This operation takes no arguments.</p>'
    return f'<form method="{method}" action="{_esc(action)}">{fields}<button type="submit">{_esc(submit_label)}</button></form>'


def render_page(catalog: Catalog, title: str, body: str, crumbs: list[tuple[str, str]] | None = None) -> str:
    task = catalog.task
    nav = [("/", "Task"), (f"{APP_PREFIX}/task", "Discovery"), (f"{APP_PREFIX}/submit", "Submit answer"), (API_PREFIX, "REST")]
    nav_html = "".join(f'<a href="{_esc(href)}">{_esc(label)}</a>' for href, label in nav)
    systems = "".join(
        f'<a href="{APP_PREFIX}/{_esc(server)}">{_esc(server)}</a>' for server in catalog.server_order if server != "hubbench"
    )
    crumb_html = ""
    if crumbs:
        crumb_html = '<p class="crumbs">' + " / ".join(f'<a href="{_esc(href)}">{_esc(label)}</a>' for href, label in crumbs) + "</p>"
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{_esc(title)} · HubBench {_esc(catalog.family.name)}</title><style>{_CSS}</style></head><body>"
        f"<nav>{nav_html}</nav><nav class=\"hint\">Systems: {systems}</nav>{crumb_html}<h1>{_esc(title)}</h1>{body}"
        f'<footer class="hint">HubBench {_esc(catalog.family.name)} · task {_esc(task["task_id"])} · one durable world shared with the MCP and CLI surfaces.</footer>'
        "</body></html>"
    )


# --------------------------------------------------------------------------- #
# HTTP server
# --------------------------------------------------------------------------- #


class _HTTPError(Exception):
    def __init__(self, status: int, message: str, *, allow: list[str] | None = None, extra: dict[str, Any] | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.allow = allow
        self.extra = extra or {}


class HubBenchHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], session: WorldSession, catalog: Catalog, *, verbose: bool = False):
        super().__init__(address, RequestHandler)
        self.session = session
        self.catalog = catalog
        self.verbose = verbose
        self.mcp_session_id = uuid.uuid4().hex

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}"


class RequestHandler(BaseHTTPRequestHandler):
    server: HubBenchHTTPServer
    server_version = "HubBenchWorld/1.0"
    protocol_version = "HTTP/1.1"

    # -- plumbing ---------------------------------------------------------- #

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        if self.server.verbose:
            sys.stderr.write(f"hubbench http: {format % args}\n")

    def _send(self, status: int, body: bytes, content_type: str, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_json(self, status: int, payload: Any, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", headers)

    def _mcp_headers(self) -> dict[str, str]:
        return {"MCP-Protocol-Version": PROTOCOL_VERSION, "Mcp-Session-Id": self.server.mcp_session_id}

    def _send_page(self, status: int, title: str, body: str, crumbs: list[tuple[str, str]] | None = None) -> None:
        page = render_page(self.server.catalog, title, body, crumbs)
        self._send(status, page.encode("utf-8"), "text/html; charset=utf-8")

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as exc:
            raise _HTTPError(400, "invalid Content-Length header") from exc
        if length > MAX_REQUEST_BYTES:
            raise _HTTPError(413, f"request body exceeds {MAX_REQUEST_BYTES} bytes")
        return self.rfile.read(length) if length > 0 else b""

    def _read_json_object(self) -> dict[str, Any]:
        body = self._read_body()
        if not body.strip():
            return {}
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _HTTPError(400, f"request body must be valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise _HTTPError(400, "request body must be a JSON object of tool arguments")
        return payload

    def _read_form(self) -> dict[str, Any]:
        content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type == "application/json":
            return self._read_json_object()
        body = self._read_body()
        if content_type not in {"", "application/x-www-form-urlencoded"}:
            raise _HTTPError(400, f"unsupported form encoding {content_type!r}; send application/x-www-form-urlencoded or application/json")
        try:
            return parse_qs(body.decode("utf-8"), keep_blank_values=True)
        except UnicodeDecodeError as exc:
            raise _HTTPError(400, "form body must be UTF-8") from exc

    @staticmethod
    def _require(method: str, *allowed: str) -> None:
        if method not in allowed:
            raise _HTTPError(405, f"method {method} not allowed here; use {' or '.join(allowed)}", allow=list(allowed))

    def _invoke(self, route: ToolRoute, arguments: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Run one tool through the world and map its outcome to a status code."""

        result = self.server.session.call_tool(route.name, arguments)
        if "error" not in result:
            return 200, result
        if result.get("retryable"):
            return 503, result
        try:
            validate_schema(arguments, route.input_schema)
        except ValueError:
            return 400, result
        if route.name == SUBMIT_TOOL:
            return 400, result
        return 422, result

    # -- dispatch ---------------------------------------------------------- #

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        if len(path) > 1:
            path = path.rstrip("/") or "/"
        query = parse_qs(parsed.query, keep_blank_values=True)
        try:
            if path == "/health":
                self._require(method, "GET")
                task = self.server.session.task
                self._send_json(200, {"status": "ok", "family": self.server.catalog.family.slug, "task_id": task["task_id"]})
            elif path == MCP_PREFIX or path.startswith(MCP_PREFIX + "/"):
                self._mcp(method, path)
            elif path == API_PREFIX or path.startswith(API_PREFIX + "/"):
                self._api(method, path, query)
            elif path in {"/", APP_PREFIX} or path.startswith(APP_PREFIX + "/"):
                self._web(method, path, query)
            else:
                raise _HTTPError(404, f"no such route: {path}")
        except _HTTPError as exc:
            self._send_http_error(exc, path, method)
        except Exception as exc:  # noqa: BLE001 - never leak a traceback to the client
            sys.stderr.write(f"hubbench http: {method} {path} failed: {type(exc).__name__}: {exc}\n")
            self._send_http_error(_HTTPError(500, "internal error"), path, method)

    def _send_http_error(self, exc: _HTTPError, path: str, method: str) -> None:
        headers = {"Allow": ", ".join(exc.allow)} if exc.allow else {}
        if path == MCP_PREFIX or path.startswith(MCP_PREFIX + "/"):
            headers.update(self._mcp_headers())
        if path.startswith(MCP_PREFIX) or path.startswith(API_PREFIX):
            self._send_json(exc.status, {"error": exc.message, "status": exc.status, **exc.extra}, headers)
            return
        reason = HTTPStatus(exc.status).phrase if exc.status in HTTPStatus.__members__.values() else "Error"
        extra = "".join(f"<dt>{_esc(key)}</dt><dd>{render_value(value)}</dd>" for key, value in exc.extra.items())
        body = f'<p class="error">{_esc(exc.message)}</p>' + (f"<dl>{extra}</dl>" if extra else "") + f'<p><a href="/">Back to the task</a></p>'
        page = render_page(self.server.catalog, f"{exc.status} {reason}", body)
        self._send(exc.status, page.encode("utf-8"), "text/html; charset=utf-8", headers)

    # -- MCP over streamable HTTP ----------------------------------------- #

    def _mcp(self, method: str, path: str) -> None:
        catalog = self.server.catalog
        server = None if path == MCP_PREFIX else path[len(MCP_PREFIX) + 1 :]
        if server is not None and server not in catalog.servers:
            raise _HTTPError(404, f"unknown MCP server {server!r}", extra={"servers": [f"{MCP_PREFIX}/{name}" for name in catalog.server_order]})
        if method != "POST":
            raise _HTTPError(405, "MCP over streamable HTTP: POST JSON-RPC 2.0 messages here (no SSE stream is offered)", allow=["POST"])
        body = self._read_body()
        try:
            payload = json.loads(body) if body.strip() else None
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._send_json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"parse error: {exc}"}}, self._mcp_headers())
            return
        if not isinstance(payload, (dict, list)):
            self._send_json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request: expected a JSON-RPC object or batch"}}, self._mcp_headers())
            return
        response = self.server.session.rpc(payload, server)
        if response is None:
            self._send(202, b"", "application/json; charset=utf-8", self._mcp_headers())
            return
        self._send_json(200, response, self._mcp_headers())

    # -- REST -------------------------------------------------------------- #

    def _api_index(self) -> dict[str, Any]:
        catalog = self.server.catalog
        task = self.server.session.task
        return {
            "benchmark": "HubBench",
            "family": catalog.family.slug,
            "name": catalog.family.name,
            "task_id": task["task_id"],
            "servers": [catalog.server_summary(server) for server in catalog.server_order],
            "endpoints": {
                "tools": f"GET {API_PREFIX}/tools",
                "tool_schema": f"GET {API_PREFIX}/tools/<name>",
                "tool_call": f"POST {API_PREFIX}/tools/<name>",
                "task": f"GET {API_PREFIX}/task",
                "submit": f"POST {API_PREFIX}/submit",
                "collection": f"GET {API_PREFIX}/<server>/<resource>",
                "item": f"GET {API_PREFIX}/<server>/<resource>/<id>",
                "operation": f"GET|POST {API_PREFIX}/<server>/<resource>/<operation>",
            },
            "mcp": {"all_tools": f"POST {MCP_PREFIX}", "per_server": f"POST {MCP_PREFIX}/<server>", "servers": [f"{MCP_PREFIX}/{name}" for name in catalog.server_order]},
            "web": "/",
        }

    def _api(self, method: str, path: str, query: dict[str, list[str]]) -> None:
        catalog = self.server.catalog
        segments = [segment for segment in path[len(API_PREFIX) :].split("/") if segment]
        if not segments:
            self._require(method, "GET")
            self._send_json(200, self._api_index())
            return
        head = segments[0]
        if head == "tools":
            if len(segments) == 1:
                self._require(method, "GET")
                self._send_json(200, {"tools": [route.catalog_entry() for route in catalog.tools.values()]})
                return
            if len(segments) == 2:
                route = catalog.tools.get(segments[1])
                if route is None:
                    raise _HTTPError(404, f"unknown tool: {segments[1]}", extra={"tools": f"GET {API_PREFIX}/tools"})
                if method == "GET":
                    self._send_json(200, route.catalog_entry())
                    return
                self._require(method, "GET", "POST")
                status, result = self._invoke(route, self._read_json_object())
                self._send_json(status, result)
                return
            raise _HTTPError(404, f"no such route: {path}")
        if head == "task" and len(segments) == 1:
            self._require(method, "GET")
            status, result = self._invoke(catalog.tools[CONTEXT_TOOL], {})
            self._send_json(status, result)
            return
        if head == "submit" and len(segments) == 1:
            self._require(method, "POST")
            status, result = self._invoke(catalog.tools[SUBMIT_TOOL], self._read_json_object())
            self._send_json(status, result)
            return
        server = head
        if server not in catalog.servers:
            raise _HTTPError(404, f"unknown server: {server}", extra={"servers": catalog.server_order})
        if len(segments) == 1:
            self._require(method, "GET")
            self._send_json(200, catalog.server_summary(server))
            return
        resource = segments[1]
        resources = catalog.resources(server)
        if len(segments) == 2:
            if resource in resources:
                self._require(method, "GET")
                route = catalog.collection(server, resource)
                if route is None:
                    raise _HTTPError(
                        404,
                        f"{server}/{resource} has no list or search tool",
                        extra={"operations": {operation: route.operation_path for operation, route in resources[resource].items()}},
                    )
                status, result = self._invoke(route, coerce_arguments(query, route.input_schema))
                self._send_json(status, result)
                return
            shorthand = catalog.item_shorthand(server, resource)
            if shorthand is not None:
                self._require(method, "GET")
                self._send_item(*shorthand, query)
                return
            raise _HTTPError(404, f"unknown resource {server}/{resource}", extra={"resources": sorted(resources)})
        if len(segments) == 3:
            tail = segments[2]
            operations = resources.get(resource)
            if operations is None:
                raise _HTTPError(404, f"unknown resource {server}/{resource}", extra={"resources": sorted(resources)})
            if tail in operations:
                route = operations[tail]
                if route.is_read:
                    self._require(method, "GET", "POST")
                    arguments = coerce_arguments(query, route.input_schema) if method == "GET" else self._read_json_object()
                else:
                    self._require(method, "POST")
                    arguments = self._read_json_object()
                status, result = self._invoke(route, arguments)
                self._send_json(status, result)
                return
            self._require(method, "GET")
            route = catalog.item(server, resource)
            if route is None:
                raise _HTTPError(
                    404,
                    f"{server}/{resource} has no get tool; {tail!r} is not an operation either",
                    extra={"operations": {operation: route.operation_path for operation, route in operations.items()}},
                )
            self._send_item(route, tail, query)
            return
        raise _HTTPError(404, f"no such route: {path}")

    def _send_item(self, route: ToolRoute, identifier: str, query: dict[str, list[str]]) -> None:
        arguments = coerce_arguments(query, route.input_schema)
        arguments[route.id_parameter or "id"] = identifier
        status, result = self._invoke(route, arguments)
        self._send_json(404 if status == 422 else status, result)

    # -- website ----------------------------------------------------------- #

    def _link_for(self, server: str, resource: str) -> Callable[[dict[str, Any]], str | None] | None:
        item = self.server.catalog.item(server, resource)
        if item is None:
            return None

        def link(row: dict[str, Any]) -> str | None:
            identifier = row.get(item.id_parameter or "id", row.get("id"))
            if isinstance(identifier, (str, int)) and not isinstance(identifier, bool):
                return f"{APP_PREFIX}/{server}/{resource}/{quote(str(identifier), safe='')}"
            return None

        return link

    def _readback(self, route: ToolRoute, result: dict[str, Any]) -> tuple[str, str]:
        catalog = self.server.catalog
        item = catalog.item(route.server, route.resource)
        if item is not None:
            identifier = result.get(item.id_parameter or "id", result.get("id"))
            if isinstance(identifier, (str, int)) and not isinstance(identifier, bool):
                return f"{APP_PREFIX}/{route.server}/{route.resource}/{quote(str(identifier), safe='')}", f"Read back {route.resource} {identifier}"
        if catalog.collection(route.server, route.resource) is not None:
            return f"{APP_PREFIX}/{route.server}/{route.resource}", f"Back to the {route.resource} listing"
        return f"{APP_PREFIX}/{route.server}", f"Back to {route.server}"

    def _result_block(self, status: int, result: dict[str, Any], link_for: Callable[[dict[str, Any]], str | None] | None = None) -> str:
        if status == 200:
            banner = '<p class="ok">OK — the call succeeded and is recorded in the world.</p>'
        else:
            banner = f'<p class="error">HTTP {status}: {_esc(result.get("error", "the call failed"))}</p>'
        return banner + render_value(result, link_for=link_for) + f"<details><summary>Raw JSON</summary>{_json_pre(result)}</details>"

    def _web(self, method: str, path: str, query: dict[str, list[str]]) -> None:
        catalog = self.server.catalog
        segments = [segment for segment in path[len(APP_PREFIX) :].split("/") if segment] if path.startswith(APP_PREFIX) else []
        if not segments:
            self._require(method, "GET")
            self._send_page(200, catalog.task["title"], self._home_body())
            return
        if segments == ["task"]:
            self._require(method, "GET")
            status, result = self._invoke(catalog.tools[CONTEXT_TOOL], {})
            body = '<p class="hint">Output of the <code>hubbench.context.get</code> discovery control.</p>' + self._result_block(status, result)
            self._send_page(status, "Task discovery", body, [("/", "Task")])
            return
        if segments == ["submit"]:
            self._require(method, "GET", "POST")
            self._web_operation(catalog.tools[SUBMIT_TOOL], method, query, title="Submit the structured answer", crumbs=[("/", "Task")])
            return
        server = segments[0]
        if server not in catalog.servers:
            raise _HTTPError(404, f"unknown system {server!r}", extra={"systems": catalog.server_order})
        crumbs = [("/", "Task"), (f"{APP_PREFIX}/{server}", server)]
        if len(segments) == 1:
            self._require(method, "GET")
            self._send_page(200, f"{server} — {catalog.descriptions.get(server, '')}", self._server_body(server), crumbs[:-1])
            return
        resource = segments[1]
        resources = catalog.resources(server)
        if len(segments) == 2:
            if resource in resources:
                self._require(method, "GET")
                self._web_listing(server, resource, query, crumbs)
                return
            shorthand = catalog.item_shorthand(server, resource)
            if shorthand is not None:
                self._require(method, "GET")
                route, identifier = shorthand
                self._web_detail(route, identifier, query, crumbs + [(f"{APP_PREFIX}/{server}/{server}", server)])
                return
            raise _HTTPError(404, f"unknown resource {server}/{resource}", extra={"resources": sorted(resources)})
        if len(segments) == 3:
            tail = segments[2]
            operations = resources.get(resource)
            if operations is None:
                raise _HTTPError(404, f"unknown resource {server}/{resource}", extra={"resources": sorted(resources)})
            crumbs = crumbs + [(f"{APP_PREFIX}/{server}/{resource}", resource)]
            if tail in operations:
                route = operations[tail]
                self._require(method, "GET", "POST")
                self._web_operation(route, method, query, title=f"{route.name} ({route.hint})", crumbs=crumbs)
                return
            self._require(method, "GET")
            route = catalog.item(server, resource)
            if route is None:
                raise _HTTPError(404, f"{server}/{resource} has no get tool; {tail!r} is not an operation either", extra={"operations": sorted(operations)})
            self._web_detail(route, tail, query, crumbs)
            return
        raise _HTTPError(404, f"no such page: {path}")

    def _home_body(self) -> str:
        catalog = self.server.catalog
        task = catalog.task
        organization = task.get("world", {})
        paragraphs = "".join(f"<p>{_esc(part.strip())}</p>" for part in str(task["instruction"]).split("\n\n") if part.strip())
        rows = []
        for server in catalog.server_order:
            routes = [route for operations in catalog.resources(server).values() for route in operations.values()]
            reads = sum(route.is_read for route in routes)
            href = f"{APP_PREFIX}/task" if server == "hubbench" else f"{APP_PREFIX}/{server}"
            rows.append(
                f'<tr><td><a href="{_esc(href)}">{_esc(server)}</a></td><td>{_esc(catalog.descriptions.get(server, ""))}</td>'
                f"<td>{reads} read · {len(routes) - reads} write</td><td><code>POST {MCP_PREFIX}/{_esc(server)}</code></td></tr>"
            )
        return (
            f'<p class="hint">{_esc(catalog.family.name)} · {_esc(organization.get("name", ""))} · role <code>{_esc(task["role"])}</code> · as of {_esc(task["as_of"])}</p>'
            f"<h2>Brief</h2>{paragraphs}"
            "<h2>Connected systems</h2><table><thead><tr><th>System</th><th>Description</th><th>Tools</th><th>MCP endpoint</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>"
            "<h2>Other surfaces</h2><ul>"
            f'<li>Discovery control: <a href="{APP_PREFIX}/task">{APP_PREFIX}/task</a> · answer form: <a href="{APP_PREFIX}/submit">{APP_PREFIX}/submit</a></li>'
            f'<li>REST: <a href="{API_PREFIX}">{API_PREFIX}</a> (catalog at <a href="{API_PREFIX}/tools">{API_PREFIX}/tools</a>)</li>'
            f"<li>MCP over streamable HTTP: <code>POST {MCP_PREFIX}</code> for every tool, <code>POST {MCP_PREFIX}/&lt;server&gt;</code> per system</li>"
            "<li>Terminal: <code>HUBBENCH_URL=&lt;this origin&gt; tool list</code></li></ul>"
        )

    def _server_body(self, server: str) -> str:
        catalog = self.server.catalog
        rows = []
        for resource, operations in catalog.resources(server).items():
            collection = catalog.collection(server, resource)
            item = catalog.item(server, resource)
            listing = f'<a href="{APP_PREFIX}/{_esc(server)}/{_esc(resource)}">{_esc(resource)}</a>' if collection else _esc(resource)
            detail = f"<code>{APP_PREFIX}/{_esc(server)}/{_esc(resource)}/&lt;{_esc(item.id_parameter)}&gt;</code>" if item else '<span class="hint">—</span>'
            links = " · ".join(
                f'<a class="{route.hint}" href="{_esc(route.web_path)}">{_esc(operation)}</a> <span class="hint">({route.hint})</span>'
                for operation, route in operations.items()
            )
            rows.append(f"<tr><td>{listing}</td><td>{detail}</td><td>{links}</td></tr>")
        return (
            f'<p class="hint">MCP: <code>POST {MCP_PREFIX}/{_esc(server)}</code> · REST: <a href="{API_PREFIX}/{_esc(server)}">{API_PREFIX}/{_esc(server)}</a></p>'
            "<table><thead><tr><th>Resource</th><th>Detail route</th><th>Operations</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>"
        )

    def _web_listing(self, server: str, resource: str, query: dict[str, list[str]], crumbs: list[tuple[str, str]]) -> None:
        catalog = self.server.catalog
        route = catalog.collection(server, resource)
        operations = catalog.resources(server)[resource]
        ops = " · ".join(
            f'<a class="{item.hint}" href="{_esc(item.web_path)}">{_esc(operation)}</a> <span class="hint">({item.hint})</span>'
            for operation, item in operations.items()
        )
        body = f"<p>Operations: {ops}</p>"
        if route is None:
            body += '<p class="hint">This resource has no list or search tool; use the operations above.</p>'
            self._send_page(200, f"{server} / {resource}", body, crumbs)
            return
        arguments = coerce_arguments(query, route.input_schema)
        required = route.input_schema.get("required", [])
        body += f"<h2>Filter — <code>{_esc(route.name)}</code></h2>" + render_form(route.input_schema, f"{APP_PREFIX}/{server}/{resource}", "get", arguments, "Search")
        status = 200
        if arguments or not required:
            status, result = self._invoke(route, arguments)
            body += "<h2>Results</h2>" + self._result_block(status, result, self._link_for(server, resource))
        else:
            body += f'<p class="hint">Provide {", ".join(f"<code>{_esc(name)}</code>" for name in required)} to run the search.</p>'
        self._send_page(status, f"{server} / {resource}", body, crumbs)

    def _web_detail(self, route: ToolRoute, identifier: str, query: dict[str, list[str]], crumbs: list[tuple[str, str]]) -> None:
        catalog = self.server.catalog
        arguments = coerce_arguments(query, route.input_schema)
        arguments[route.id_parameter or "id"] = identifier
        status, result = self._invoke(route, arguments)
        status = 404 if status == 422 else status
        body = f'<p class="hint">Read through <code>{_esc(route.name)}</code>.</p>' + self._result_block(status, result)
        writes = [item for operation, item in catalog.resources(route.server)[route.resource].items() if not item.is_read]
        if writes:
            links = []
            for item in writes:
                prefill = f"?{quote(route.id_parameter or 'id')}={quote(identifier, safe='')}" if (route.id_parameter or "id") in item.input_schema.get("properties", {}) else ""
                links.append(f'<li><a class="write" href="{_esc(item.web_path + prefill)}">{_esc(item.name)}</a> — {_esc(item.description)}</li>')
            body += "<h2>Actions</h2><ul>" + "".join(links) + "</ul>"
        self._send_page(status, f"{route.resource} {identifier}", body, crumbs)

    def _web_operation(self, route: ToolRoute, method: str, query: dict[str, list[str]], *, title: str, crumbs: list[tuple[str, str]]) -> None:
        action = f"{APP_PREFIX}/submit" if route.name == SUBMIT_TOOL else route.web_path
        body = f'<p class="hint">{_esc(route.description)}</p>'
        if route.is_read:
            arguments = coerce_arguments(query, route.input_schema) if method == "GET" else self._read_form_arguments(route)
            body += render_form(route.input_schema, action, "get", arguments, "Run")
            status = 200
            if arguments or not route.input_schema.get("required") or method == "POST":
                status, result = self._invoke(route, arguments)
                body += "<h2>Result</h2>" + self._result_block(status, result, self._link_for(route.server, route.resource))
            self._send_page(status, title, body, crumbs)
            return
        if method == "GET":
            prefill = coerce_arguments(query, route.input_schema)
            body += render_form(route.input_schema, action, "post", prefill, "Submit")
            self._send_page(200, title, body, crumbs)
            return
        arguments = self._read_form_arguments(route)
        status, result = self._invoke(route, arguments)
        body += "<h2>Result</h2>" + self._result_block(status, result)
        if status == 200:
            if route.name == SUBMIT_TOOL:
                body += f'<p><a href="/">Back to the task</a> · <a href="{APP_PREFIX}/submit">Submit again</a></p>'
            else:
                href, label = self._readback(route, result)
                body += f'<p><a href="{_esc(href)}">{_esc(label)}</a></p>'
        body += "<h2>Form</h2>" + render_form(route.input_schema, action, "post", arguments, "Submit")
        self._send_page(status, title, body, crumbs)

    def _read_form_arguments(self, route: ToolRoute) -> dict[str, Any]:
        values = self._read_form()
        if values and all(isinstance(value, list) for value in values.values()):
            return coerce_arguments(values, route.input_schema)
        return dict(values)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def build_server(family: Family, task: dict[str, Any], database: str | Path, *, host: str = "127.0.0.1", port: int = 8765, fresh: bool = False, verbose: bool = False) -> HubBenchHTTPServer:
    session = WorldSession(family, task, database, fresh=fresh)
    try:
        return HubBenchHTTPServer((host, port), session, Catalog(family, task), verbose=verbose)
    except BaseException:
        session.close()
        raise


def announce(server: HubBenchHTTPServer, database: str | Path) -> dict[str, Any]:
    catalog = server.catalog
    return {
        "hubbench": "http",
        "family": catalog.family.slug,
        "task_id": catalog.task["task_id"],
        "db": str(database),
        "url": server.url,
        "mcp": [f"{MCP_PREFIX}"] + [f"{MCP_PREFIX}/{name}" for name in catalog.server_order],
        "rest": API_PREFIX,
        "web": "/",
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Serve one HubBench task world over HTTP: MCP (streamable HTTP), REST, and a website")
    parser.add_argument("--family", default="clinicops")
    parser.add_argument("--task", required=True, help="task id or task JSON path")
    parser.add_argument("--db", type=Path, required=True, help="SQLite world file (shared with the stdio server and the tool CLI)")
    parser.add_argument("--fresh", action="store_true", help="reseed the world even if the database exists")
    parser.add_argument("--release", type=Path, default=None, help="release directory holding tasks/<id>.json")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765, help="TCP port (0 picks a free one; the bound URL is printed on stdout)")
    parser.add_argument("--verbose", action="store_true", help="log each request to stderr")
    args = parser.parse_args(argv)
    family = load_family(args.family)
    task = load_task(family, args.task, release_dir=args.release)
    server = build_server(family, task, args.db, host=args.host, port=args.port, fresh=args.fresh, verbose=args.verbose)
    print(json.dumps(announce(server, args.db), sort_keys=True), flush=True)

    def _terminate(*_: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _terminate)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        server.session.close()


__all__ = ["Catalog", "HubBenchHTTPServer", "RequestHandler", "ToolRoute", "WorldSession", "announce", "build_server", "coerce_arguments", "main", "render_page", "render_value"]


if __name__ == "__main__":
    main()
