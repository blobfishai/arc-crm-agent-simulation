"""Reference solver, mounted only for an oracle trial; never an agent image input."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/solution"))
    parser.add_argument("--client", type=Path, default=Path("/opt/arc-client/client.py"))
    parser.add_argument("--log", type=Path, default=Path("/logs/agent/surfaces.json"))
    parser.add_argument("--url", default="http://world:8765")
    parser.add_argument("--require-uid", type=int, default=10001)
    args = parser.parse_args(argv)
    if os.getuid() != args.require_uid:
        raise ValueError("oracle must run under the same unprivileged account as a model agent")
    sys.path.insert(0, str(args.client.parent))
    from client import call, request

    steps = json.loads((args.root / "steps.json").read_text())
    definitions = request(args.url.rstrip("/") + "/api/v1/tools")["tools"]
    writes = {item["name"] for item in definitions if item["hint"] == "write"} - {"hubbench.submit_answer"}
    events, write_surfaces = [], []
    for index, step in enumerate(steps):
        surface = ("rest", "cli", "mcp", "web")[index % 4]
        if step["tool"] in writes:
            surface = ("web", "mcp", "cli")[len(write_surfaces) % 3]
            write_surfaces.append(surface)
            if surface == "web":
                page = request(args.url.rstrip("/") + "/app/" + step["tool"].replace(".", "/"))
                if "<form" not in page:
                    raise ValueError("mutation form missing")
        if step["tool"] == "hubbench.context.get":
            surface = "rest"
        if surface == "cli":
            command = [sys.executable, "-I", "-S", "-B", str(args.client), "--url", args.url, step["tool"], json.dumps(step["arguments"])]
            completed = subprocess.run(command, check=True, text=True, capture_output=True, timeout=20)
            result = json.loads(completed.stdout)
        else:
            result = call(args.url, step["tool"], step["arguments"], surface=surface, request_id=index + 1)
        if not isinstance(result, dict) or "error" in result:
            raise ValueError("oracle encountered a failed tool result")
        raw = json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        events.append({"index": index, "tool": step["tool"], "surface": surface, "result_sha256": hashlib.sha256(raw).hexdigest()})
    if {entry["surface"] for entry in events} != {"cli", "web", "rest", "mcp"} or "web" not in write_surfaces:
        raise ValueError("oracle did not execute all four surfaces and an HTML-form mutation")
    args.log.parent.mkdir(parents=True, exist_ok=True)
    with args.log.open("x") as handle:
        json.dump({"uid": os.getuid(), "events": events, "write_surfaces": write_surfaces, "tool_errors": 0}, handle, sort_keys=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
