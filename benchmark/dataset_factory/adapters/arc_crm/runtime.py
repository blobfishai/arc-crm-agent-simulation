"""Local shared CLI/web/REST/MCP runtime. Never silently reset an existing world."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from benchmark.hubbench.engine import world as engine_world
from benchmark.hubbench.engine.cli import _remote_main
from benchmark.hubbench.engine.http import build_server
from benchmark.hubbench.engine.server import serve
from benchmark.hubbench.engine.validation import canonical_json
from benchmark.hubbench.engine.verifier import verify_episode
from benchmark.hubbench.engine.world import World, seed_database

from . import FAMILY, ROOT, SOURCE_LOCK, build_tasks


def implementation_hash() -> str:
    sources = {}
    for label, directory in [
        ("arc_crm", ROOT),
        ("hubbench_engine", Path(engine_world.__file__).parent),
    ]:
        for path in sorted(directory.iterdir()):
            if path.is_file() and path.suffix in {".py", ".sql", ".json"}:
                sources[f"{label}/{path.name}"] = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
    return hashlib.sha256(canonical_json(sources).encode()).hexdigest()


def load_task(task_id: str) -> dict:
    task = next((task for task in build_tasks() if task["task_id"] == task_id), None)
    if task is None:
        raise ValueError(f"unknown Arc CRM task: {task_id}")
    return task


def session_database(directory: str | Path, task: dict) -> Path:
    """Create a new owned directory or verify an exact existing session binding.

    No reset/overwrite option. A partial, unowned, symlinked, wrong-task, or
    different-code session is rejected. Use a new directory for a new episode.
    This is local lifecycle safety, not a sandbox against a hostile filesystem owner.
    """
    directory = Path(directory).expanduser().absolute()
    if directory.is_symlink() or directory.name in {"", ".", ".."}:
        raise ValueError("session directory must not be a symlink or broad root")
    directory = directory.parent.resolve(strict=True) / directory.name
    binding = {
        "adapter": SOURCE_LOCK["adapter"],
        "version": FAMILY.version,
        "task_id": task["task_id"],
        "task_sha256": hashlib.sha256(canonical_json(task).encode()).hexdigest(),
        "implementation_sha256": implementation_hash(),
    }
    database, identity = directory / "world.sqlite", directory / "identity.json"
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        if (
            not directory.is_dir()
            or directory.is_symlink()
            or identity.is_symlink()
            or database.is_symlink()
        ):
            raise ValueError(
                "existing session is not a regular owned directory"
            ) from None
        allowed = {
            "world.sqlite",
            "world.sqlite-journal",
            "world.sqlite-wal",
            "world.sqlite-shm",
            "identity.json",
        }
        if any(
            path.is_symlink() or path.name not in allowed
            for path in directory.iterdir()
        ):
            raise ValueError("existing session contains unowned or symlinked files")
        if (
            not identity.is_file()
            or not database.is_file()
            or json.loads(identity.read_text()) != binding
        ):
            raise ValueError(
                "existing session identity, task, code, or database does not match; choose a new directory"
            )
    else:
        with identity.open("x", encoding="utf-8") as handle:
            handle.write(canonical_json(binding) + "\n")
        seed_database(FAMILY, task, database)
    return database


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("serve", "stdio", "tool", "verify"):
        child = commands.add_parser(name)
        child.add_argument("--task")
        child.add_argument("--session", type=Path)
        if name == "serve":
            child.add_argument("--port", type=int, default=8766)
        if name == "tool":
            child.add_argument(
                "--url",
                help="Remote mode over the served world's REST surface; no local SQLite access",
            )
            child.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.command == "tool" and args.url:
        if args.task or args.session:
            parser.error("remote mode cannot also select a local task/session")
        if not args.arguments:
            parser.error("tool requires list, schema NAME, or NAME '{json}'")
        return _remote_main(args.arguments, args.url)
    if not args.task or not args.session:
        parser.error("local commands require --task and --session")
    task = load_task(args.task)
    database = session_database(args.session, task)
    if args.command == "serve":
        server = build_server(FAMILY, task, database, host="127.0.0.1", port=args.port)
        print(
            json.dumps(
                {
                    "adapter": SOURCE_LOCK["adapter"],
                    "task_id": task["task_id"],
                    "url": server.url,
                    "engine": "HubBench (including HTTP/MCP presentation and context/answer controls)",
                    "surfaces": ["CLI", "web", "REST", "MCP"],
                }
            ),
            flush=True,
        )
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
            server.session.close()
        return 0
    with World(FAMILY, task, database) as world:
        if args.command == "stdio":
            serve(world)
            return 0
        if args.command == "verify":
            verdict = verify_episode(task, world)
            print(
                json.dumps(
                    {
                        "adapter": SOURCE_LOCK["adapter"],
                        "version": FAMILY.version,
                        "engine_verdict": verdict,
                    },
                    sort_keys=True,
                )
            )
            return 0 if verdict["strict_pass"] else 1
        if not args.arguments:
            parser.error("tool requires list, schema NAME, trace, or NAME '{json}'")
        command, *rest = args.arguments
        if command == "list":
            print(json.dumps(world.tool_definitions(), sort_keys=True))
            return 0
        if command == "trace":
            print(json.dumps(world.trace, sort_keys=True))
            return 0
        if command == "schema":
            definition = next(
                (
                    item
                    for item in world.tool_definitions()
                    if rest and item["name"] == rest[0]
                ),
                None,
            )
            if definition is None:
                raise ValueError("schema requires a known tool name")
            print(json.dumps(definition["inputSchema"], sort_keys=True))
            return 0
        if command == "call":
            if not rest:
                raise ValueError("call requires a tool name")
            command, *rest = rest
        if len(rest) > 1:
            raise ValueError("tool accepts one JSON argument object")
        arguments = json.loads(rest[0]) if rest else {}
        if not isinstance(arguments, dict):
            raise TypeError("tool arguments must be an object")
        result = world.call_tool(command, arguments)
        print(json.dumps(result, sort_keys=True))
        return int("error" in result)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (TypeError, ValueError, OSError) as error:
        print(f"arc-crm: {error}", file=sys.stderr)
        raise SystemExit(2) from error
