"""Terminal ``tool`` CLI over the same SQLite world the MCP server uses.

    tool list                      # tool names and read/write hints
    tool schema <name>             # input schema
    tool <name> '<json args>'      # call a tool (also: tool call <name> '<json>')
    tool reset                     # reseed the session world
    tool trace                     # the durable call trace of this session

Environment: HUBBENCH_FAMILY (default clinicops), HUBBENCH_TASK (task id or
task JSON path; default: first released task), HUBBENCH_DB (session database;
default ./.hubbench/<task>.db), HUBBENCH_RELEASE (release directory).

Remote mode: when HUBBENCH_URL points at a served world
(``python3 -m hubbench.engine.http`` / ``bin/serve``), ``tool list`` and
``tool schema`` read ``GET <url>/api/v1/tools`` and ``tool <name> '<json>'``
POSTs to ``<url>/api/v1/tools/<name>`` — the same durable world, no local
SQLite file needed.  ``reset`` and ``trace`` stay local-only.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .families import load_family
from .tasks import load_release_tasks, load_task, release_dir
from .world import World, seed_database


def _session() -> tuple[Any, dict[str, Any], Path]:
    family = load_family(os.environ.get("HUBBENCH_FAMILY", "clinicops"))
    release = Path(os.environ["HUBBENCH_RELEASE"]) if os.environ.get("HUBBENCH_RELEASE") else release_dir(family)
    task_ref = os.environ.get("HUBBENCH_TASK")
    if task_ref:
        task = load_task(family, task_ref, release_dir=release)
    else:
        tasks = load_release_tasks(family, release)
        if not tasks:
            raise SystemExit(f"no released tasks under {release}; set HUBBENCH_TASK")
        task = tasks[0]
    database = Path(os.environ.get("HUBBENCH_DB") or Path(".hubbench") / f"{task['task_id']}.db")
    return family, task, database


def _http(method: str, url: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, json.loads(response.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"error": raw or exc.reason}
        return exc.code, payload
    except urllib.error.URLError as exc:
        raise SystemExit(f"cannot reach HUBBENCH_URL {url}: {exc.reason}") from exc


def _remote_main(argv: list[str], base_url: str) -> int:
    """``tool`` against a served world: same commands and output, over the REST surface."""

    base = base_url.rstrip("/")
    command = argv[0]
    if command in {"reset", "trace"}:
        raise SystemExit(f"tool {command} is local-only and not available over HUBBENCH_URL")
    if command == "list":
        _, catalog = _http("GET", f"{base}/api/v1/tools")
        for item in catalog["tools"]:
            print(f"{item['name']}\t{item['hint']}\t{item['description']}")
        return 0
    if command == "schema":
        if len(argv) < 2:
            raise SystemExit("tool schema requires a tool name")
        status, entry = _http("GET", f"{base}/api/v1/tools/{argv[1]}")
        if status == 404:
            raise SystemExit(f"unknown tool: {argv[1]}")
        print(json.dumps(entry["input_schema"], indent=2, sort_keys=True))
        return 0
    if command == "call":
        argv = argv[1:]
        if not argv:
            raise SystemExit("tool call requires a tool name")
    name = argv[0]
    try:
        arguments = json.loads(argv[1]) if len(argv) > 1 else {}
    except json.JSONDecodeError as exc:
        raise SystemExit(f"arguments must be valid JSON: {exc}") from exc
    if not isinstance(arguments, dict):
        raise SystemExit("arguments must be a JSON object")
    status, result = _http("POST", f"{base}/api/v1/tools/{name}", arguments)
    if status == 404 and isinstance(result, dict) and str(result.get("error", "")).startswith("unknown tool"):
        raise SystemExit(f"unknown tool: {name}")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if isinstance(result, dict) and "error" in result else 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in {"-h", "--help", "help"}:
        print(__doc__.strip())
        return 0
    remote = os.environ.get("HUBBENCH_URL")
    if remote:
        return _remote_main(argv, remote)
    family, task, database = _session()
    command = argv[0]
    if command == "reset":
        seed_database(family, task, database)
        print(json.dumps({"reset": True, "task_id": task["task_id"], "db": str(database)}))
        return 0
    if not database.exists():
        seed_database(family, task, database)
    with World(family, task, database) as world:
        definitions = world.tool_definitions()
        if command == "list":
            for item in definitions:
                hint = item["_meta"]["hubbench"]["hint"]
                print(f"{item['name']}\t{hint}\t{item['description']}")
            return 0
        if command == "trace":
            print(json.dumps(world.trace, indent=2, sort_keys=True))
            return 0
        if command == "schema":
            if len(argv) < 2:
                raise SystemExit("tool schema requires a tool name")
            definition = next((item for item in definitions if item["name"] == argv[1]), None)
            if definition is None:
                raise SystemExit(f"unknown tool: {argv[1]}")
            print(json.dumps(definition["inputSchema"], indent=2, sort_keys=True))
            return 0
        if command == "call":
            argv = argv[1:]
            if not argv:
                raise SystemExit("tool call requires a tool name")
        name = argv[0]
        if name not in {item["name"] for item in definitions}:
            raise SystemExit(f"unknown tool: {name}")
        try:
            arguments = json.loads(argv[1]) if len(argv) > 1 else {}
        except json.JSONDecodeError as exc:
            raise SystemExit(f"arguments must be valid JSON: {exc}") from exc
        if not isinstance(arguments, dict):
            raise SystemExit("arguments must be a JSON object")
        result = world.call_tool(name, arguments)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 1 if "error" in result else 0


if __name__ == "__main__":
    sys.exit(main())
