"""Stateful stdio MCP server (JSON-RPC 2.0, one line per message) for one task world.

Run from ``benchmark/``::

    python3 -m hubbench.engine.server --family clinicops --task clinicops-001 --db /tmp/clinicops-001.db --fresh

The JSON-RPC core (``handle_request`` / ``handle_message``) is transport
independent: the stdio loop below and ``hubbench.engine.http`` (MCP over
streamable HTTP) share it, optionally scoped to one mock server so
``/mcp/<server>`` only lists and calls that server's tools.  Only the
provider-shaped tools and the two benchmark controls are exposed; the sealed
verifier contract is never reachable through the protocol.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .families import Family, load_family
from .tasks import load_task
from .world import World, seed_database

PROTOCOL_VERSION = "2025-03-26"


def _response(request_id: Any, result: Any = None, error: dict[str, Any] | None = None) -> dict[str, Any]:
    response: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        response["error"] = error
    else:
        response["result"] = result
    return response


def tool_definitions(world: World, server: str | None = None) -> list[dict[str, Any]]:
    """The world's public tool definitions, optionally only those mounted on ``server``."""

    definitions = world.tool_definitions()
    if server is None:
        return definitions
    return [definition for definition in definitions if definition["_meta"]["hubbench"]["server"] == server]


def server_name(world: World, server: str | None = None) -> str:
    base = f"hubbench-{world.family.slug}"
    return base if server is None else f"{base}-{server}"


def handle_request(world: World, request: dict[str, Any], server: str | None = None) -> dict[str, Any] | None:
    """Answer one JSON-RPC request (``None`` for notifications).

    ``server`` scopes ``tools/list``, ``tools/call``, and the tool-contract
    resource to one mock server (``"hubbench"`` carries the two controls).
    """

    if not isinstance(request, dict):
        return _response(None, error={"code": -32600, "message": "invalid request: expected a JSON-RPC object"})
    method = request.get("method")
    request_id = request.get("id")
    if method == "notifications/initialized":
        return None
    if method == "initialize":
        instructions = (
            f"HubBench {world.family.name} task {world.task['task_id']}. The world is an isolated, stateful snapshot; "
            "writes persist and readbacks reflect them. Record the structured decision with hubbench.submit_answer when the work is complete."
        )
        if server is not None:
            instructions += (
                f" This endpoint exposes the {server!r} server only; the hubbench control server carries "
                "hubbench.context.get and hubbench.submit_answer."
            )
        return _response(
            request_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {
                    "experimental": {},
                    "logging": {},
                    "prompts": {"listChanged": False},
                    "resources": {"subscribe": False, "listChanged": False},
                    "tools": {"listChanged": False},
                },
                "serverInfo": {"name": server_name(world, server), "version": world.family.version},
                "instructions": instructions,
            },
        )
    if method == "ping":
        return _response(request_id, {})
    if method == "tools/list":
        return _response(request_id, {"tools": tool_definitions(world, server)})
    if method == "tools/call":
        params = request.get("params", {})
        name = params.get("name", "")
        if server is not None and name not in {definition["name"] for definition in tool_definitions(world, server)}:
            result: dict[str, Any] = {"error": f"tool {name!r} is not exposed by server {server!r}"}
        else:
            result = world.call_tool(name, params.get("arguments", {}))
        return _response(request_id, {"content": [{"type": "text", "text": json.dumps(result, sort_keys=True)}], "isError": "error" in result})
    task_uri = f"hubbench://{world.family.slug}/tasks/{world.task['task_id']}"
    if method == "resources/list":
        return _response(
            request_id,
            {
                "resources": [
                    {"name": f"{world.task['task_id']}-instruction", "title": "Task instruction", "uri": f"{task_uri}/instruction", "mimeType": "text/plain"},
                    {"name": f"{world.task['task_id']}-tool-contract", "title": "Task-scoped MCP tool contract", "uri": f"{task_uri}/tool-contract", "mimeType": "application/json"},
                ]
            },
        )
    if method == "resources/read":
        uri = request.get("params", {}).get("uri")
        if uri == f"{task_uri}/instruction":
            contents = [{"uri": uri, "mimeType": "text/plain", "text": world.task["instruction"]}]
        elif uri == f"{task_uri}/tool-contract":
            contents = [{"uri": uri, "mimeType": "application/json", "text": json.dumps({"tools": tool_definitions(world, server)}, sort_keys=True)}]
        else:
            return _response(request_id, error={"code": -32002, "message": "resource not found"})
        return _response(request_id, {"contents": contents})
    if method == "prompts/list":
        return _response(
            request_id,
            {"prompts": [{"name": "run-hubbench-task", "title": f"Run the {world.family.name} task", "description": "Complete the current task against its isolated world.", "arguments": []}]},
        )
    if method == "prompts/get":
        if request.get("params", {}).get("name") != "run-hubbench-task":
            return _response(request_id, error={"code": -32602, "message": "prompt not found"})
        return _response(
            request_id,
            {
                "description": f"Complete the isolated {world.family.name} task.",
                "messages": [
                    {
                        "role": "user",
                        "content": {
                            "type": "text",
                            "text": f"{world.task['instruction']} Investigate the isolated world in any valid order. When the business work is complete, record the structured decision with hubbench.submit_answer.",
                        },
                    }
                ],
            },
        )
    return _response(request_id, error={"code": -32601, "message": f"method not found: {method}"})


def handle_message(world: World, payload: Any, server: str | None = None) -> dict[str, Any] | list[dict[str, Any]] | None:
    """Dispatch one decoded JSON-RPC payload: a request, a notification, or a batch.

    Returns ``None`` when nothing is to be sent back (a notification, or a
    batch made only of notifications).
    """

    if isinstance(payload, list):
        if not payload:
            return _response(None, error={"code": -32600, "message": "invalid request: empty batch"})
        responses = [response for item in payload if (response := handle_request(world, item, server)) is not None]
        return responses or None
    return handle_request(world, payload, server)


def serve(world: World, stdin=None, stdout=None) -> None:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
            response = handle_message(world, request)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            response = _response(None, error={"code": -32600, "message": str(exc)})
        if response is not None:
            stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
            stdout.flush()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Serve one HubBench task world over stdio MCP")
    parser.add_argument("--family", default="clinicops")
    parser.add_argument("--task", required=True, help="task id or task JSON path")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--fresh", action="store_true", help="reseed the world even if the database exists")
    parser.add_argument("--release", type=Path, default=None, help="release directory holding tasks/<id>.json")
    args = parser.parse_args(argv)
    family: Family = load_family(args.family)
    task = load_task(family, args.task, release_dir=args.release)
    if args.fresh or not args.db.exists():
        seed_database(family, task, args.db)
    with World(family, task, args.db) as world:
        serve(world)


if __name__ == "__main__":
    main()
