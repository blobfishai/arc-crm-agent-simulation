"""Standard-library-only public client. No engine, database, verifier or reset API."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request

CONTEXT = "hubbench.context.get"
SUBMIT = "hubbench.submit_answer"


def request(url: str, payload=None, *, form=False):
    headers = {"Accept": "application/json, text/html"}
    data = None
    if payload is not None:
        if form:
            data = urllib.parse.urlencode(
                {
                    key: json.dumps(value) if isinstance(value, (dict, list, bool)) else str(value)
                    for key, value in payload.items()
                }
            ).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(payload, allow_nan=False).encode()
            headers["Content-Type"] = "application/json"
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=15) as response:
        raw = response.read().decode()
        return raw if "text/html" in response.headers.get("Content-Type", "") else json.loads(raw)


def call(url: str, tool: str, arguments: dict, *, surface="rest", request_id=1):
    if not isinstance(arguments, dict):
        raise ValueError("tool arguments must be one JSON object")
    url = url.rstrip("/")
    if surface == "rest":
        result = request(f"{url}/api/v1/tools/{urllib.parse.quote(tool, safe='.')}", arguments)
    elif surface == "mcp":
        rpc = request(
            f"{url}/mcp/{urllib.parse.quote(tool.split('.')[0], safe='')}",
            {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {"name": tool, "arguments": arguments}},
        )
        if not isinstance(rpc, dict) or rpc.get("id") != request_id or "error" in rpc:
            raise ValueError("MCP transport returned an invalid or unsuccessful response")
        envelope = rpc["result"]
        if envelope.get("isError") is not False:
            raise ValueError("MCP tool call failed")
        content = envelope["content"]
        if len(content) != 1 or content[0]["type"] != "text":
            raise ValueError("MCP result must contain one JSON text value")
        result = json.loads(content[0]["text"])
    elif surface == "web":
        path = "/app/task" if tool == CONTEXT else "/app/submit" if tool == SUBMIT else "/app/" + tool.replace(".", "/")
        if tool == CONTEXT and arguments:
            raise ValueError("context takes no arguments")
        body = request(url + path, None if tool == CONTEXT else arguments, form=True)
        match = re.search(r"<summary>Raw JSON</summary><pre[^>]*>(.*?)</pre>", body, flags=re.DOTALL)
        if not match:
            raise ValueError("web response did not include its executed tool result")
        result = json.loads(html.unescape(match.group(1)))
    else:
        raise ValueError(f"unknown public surface: {surface}")
    if not isinstance(result, dict) or "error" in result:
        raise ValueError("public tool call failed: " + json.dumps(result))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("ARC_URL", "http://world:8765"))
    parser.add_argument("--surface", choices=["rest", "mcp", "web"], default="rest")
    parser.add_argument("command")
    parser.add_argument("arguments", nargs="*")
    args = parser.parse_args(argv)
    rest = args.arguments
    if args.command == "list":
        if rest:
            parser.error("list takes no arguments")
        result = request(args.url.rstrip("/") + "/api/v1/tools")["tools"]
    elif args.command == "schema":
        if len(rest) != 1:
            parser.error("schema requires one tool name")
        result = request(args.url.rstrip("/") + "/api/v1/tools/" + urllib.parse.quote(rest[0], safe="."))
    else:
        tool = args.command
        if tool == "call":
            if not rest:
                parser.error("call requires a tool name")
            tool, *rest = rest
        if len(rest) > 1:
            parser.error("a call accepts at most one JSON argument object")
        result = call(args.url, tool, json.loads(rest[0]) if rest else {}, surface=args.surface)
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, TypeError, KeyError, OSError) as error:
        print(f"tool: {error}", file=sys.stderr)
        raise SystemExit(2) from error
