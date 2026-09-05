import html
import json
import re
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager

import pytest

from benchmark.dataset_factory.adapters.arc_crm import FAMILY, ROOT
from benchmark.dataset_factory.adapters.arc_crm.qualification import run
from benchmark.dataset_factory.adapters.arc_crm.runtime import (
    load_task,
    session_database,
)
from benchmark.hubbench.engine.families import SUBMIT_TOOL
from benchmark.hubbench.engine.http import build_server
from benchmark.hubbench.engine.verifier import verify_episode
from benchmark.hubbench.engine.world import World

REPO = ROOT.parents[3]
MODULE = "benchmark.dataset_factory.adapters.arc_crm.runtime"


@contextmanager
def served(task, path):
    database = session_database(path, task)
    server = build_server(FAMILY, task, database, host="127.0.0.1", port=0)
    thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.02), daemon=True
    )
    thread.start()
    try:
        yield server, database
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
        server.session.close()
        assert not thread.is_alive()


def http_request(url, *, payload=None, form=False):
    if payload is None:
        request = urllib.request.Request(url)
    else:
        encoded = (
            urllib.parse.urlencode(
                {
                    key: json.dumps(value)
                    if isinstance(value, (dict, list, bool))
                    else str(value)
                    for key, value in payload.items()
                }
            ).encode()
            if form
            else json.dumps(payload).encode()
        )
        request = urllib.request.Request(
            url,
            data=encoded,
            headers={
                "Content-Type": "application/x-www-form-urlencoded"
                if form
                else "application/json"
            },
        )
    try:
        response = urllib.request.urlopen(request, timeout=10)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        raw = response.read().decode()
        return response.status, raw if "text/html" in response.headers.get(
            "Content-Type", ""
        ) else json.loads(raw)


def remote_cli(url, step):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            MODULE,
            "tool",
            "--url",
            url,
            step["tool"],
            json.dumps(step["arguments"]),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout)


def surface_call(server, surface, step, request_id=1):
    if surface == "cli":
        return remote_cli(server.url, step)
    if surface == "rest":
        status, result = http_request(
            f"{server.url}/api/v1/tools/{step['tool']}", payload=step["arguments"]
        )
        assert status == 200, result
        return result
    if surface == "mcp":
        status, result = http_request(
            server.url + "/mcp",
            payload={
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": step["tool"], "arguments": step["arguments"]},
            },
        )
        assert status == 200 and not result["result"]["isError"], result
        return json.loads(result["result"]["content"][0]["text"])
    if surface == "web":
        path = (
            "/app/submit"
            if step["tool"] == SUBMIT_TOOL
            else "/app/" + step["tool"].replace(".", "/")
        )
        status, body = http_request(
            server.url + path, payload=step["arguments"], form=True
        )
        assert status == 200, body
        raw = re.search(
            r"<summary>Raw JSON</summary><pre[^>]*>(.*?)</pre>", body, flags=re.DOTALL
        )
        assert raw, body
        return json.loads(html.unescape(raw.group(1)))
    raise AssertionError(surface)


@pytest.mark.parametrize("number", range(1, 7))
def test_continuous_episode_shared_by_cli_web_rest_mcp(number, tmp_path):
    task = load_task(f"arc-crm-{number:03d}")
    expected = run(task, task["oracle_steps"], tmp_path / "reference.sqlite")
    with served(task, tmp_path / "shared") as (server, database):
        status, body = http_request(server.url + "/")
        assert status == 200 and task["title"] in body and "clean-room" in body
        assert "oracle_steps" not in body and "expected_result_contains" not in body
        status, catalog = http_request(server.url + "/api/v1/tools")
        assert status == 200 and len(catalog["tools"]) == 33
        used, write_surfaces = set(), []
        for index, step in enumerate(task["oracle_steps"]):
            surface = ("rest", "cli", "mcp", "web")[index % 4]
            if step["tool"] in FAMILY.write_tools - {SUBMIT_TOOL}:
                surface = ("web", "mcp", "cli")[len(write_surfaces) % 3]
                write_surfaces.append(surface)
                if surface == "web":
                    status, form = http_request(
                        server.url + "/app/" + step["tool"].replace(".", "/")
                    )
                    assert status == 200 and "<form" in form
            if step["tool"] == "hubbench.context.get":
                surface = "rest"
            actual = surface_call(server, surface, step, request_id=index + 1)
            assert actual == expected["trace"][index]["result"]
            used.add(surface)
        assert used == {"cli", "web", "rest", "mcp"}
        assert "web" in write_surfaces  # a real submitted mutation, not a rendered form
        # Fresh reader observes the durable HTTP/CLI/MCP trace before verifying.
        with World(FAMILY, task, database) as world:
            assert world.trace == expected["trace"]
            assert world.snapshot() == expected["final_state"]
            assert verify_episode(task, world) == expected["verdict"]


def test_local_cli_stdio_and_http_reopen_the_same_binding(tmp_path):
    task = load_task("arc-crm-001")
    folder = tmp_path / "shared"
    common = ["--task", task["task_id"], "--session", str(folder)]
    command = [sys.executable, "-m", MODULE]
    first = subprocess.run(
        [*command, "tool", *common, "hubbench.context.get", "{}"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert first.returncode == 0, first.stderr
    rpc = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "arccrm.clients.get", "arguments": {"client_id": "C-11"}},
    }
    second = subprocess.run(
        [*command, "stdio", *common],
        input=json.dumps(rpc) + "\n",
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert second.returncode == 0, second.stderr
    assert not json.loads(second.stdout)["result"]["isError"]
    with served(task, folder) as (server, database):
        surface_call(
            server,
            "rest",
            {"tool": "arccrm.contacts.search", "arguments": {"query": "C-11"}},
        )
        with World(FAMILY, task, database) as world:
            assert [entry["tool"] for entry in world.trace] == [
                "hubbench.context.get",
                "arccrm.clients.get",
                "arccrm.contacts.search",
            ]


@pytest.mark.parametrize("surface", ["rest", "web", "mcp", "cli"])
def test_same_validation_and_rollback_on_every_surface(surface, tmp_path):
    task = load_task("arc-crm-002")
    bad = {
        "opportunity_id": "OP-21",
        "stage": "negotiation",
        "probability": 70,
        "expected_revision": 4,
    }
    with served(task, tmp_path / "episode") as (server, database):
        with World(FAMILY, task, database) as world:
            initial = world.snapshot()
        if surface == "rest":
            status, result = http_request(
                server.url + "/api/v1/tools/arccrm.opportunities.update", payload=bad
            )
            assert status == 422 and "stage/probability" in result["error"]
        elif surface == "web":
            status, body = http_request(
                server.url + "/app/arccrm/opportunities/update", payload=bad, form=True
            )
            assert status == 422 and "stage/probability" in body
        elif surface == "mcp":
            status, result = http_request(
                server.url + "/mcp/arccrm",
                payload={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "arccrm.opportunities.update", "arguments": bad},
                },
            )
            assert status == 200 and result["result"]["isError"]
        else:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    MODULE,
                    "tool",
                    "--url",
                    server.url,
                    "arccrm.opportunities.update",
                    json.dumps(bad),
                ],
                cwd=REPO,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            assert (
                result.returncode == 1
                and "stage/probability" in json.loads(result.stdout)["error"]
            )
        with World(FAMILY, task, database) as world:
            assert world.snapshot() == initial
            assert len(world.trace) == 1 and not world.trace[0]["success"]


def test_mcp_discovery_is_scoped_and_sealed_contract_is_unreachable(tmp_path):
    task = load_task("arc-crm-003")
    with served(task, tmp_path / "episode") as (server, _database):
        status, response = http_request(
            server.url + "/mcp/vault",
            payload={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        assert status == 200
        assert [tool["name"] for tool in response["result"]["tools"]] == [
            "vault.files.get"
        ]
        status, response = http_request(
            server.url + "/mcp/vault",
            payload={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "arccrm.quotes.create", "arguments": {}},
            },
        )
        assert response["result"]["isError"]
        for path in [
            "/sealed/arc-crm-003.json",
            "/api/v1/verifier",
            "/api/v1/expected",
            "/api/v1/oracle_steps",
        ]:
            status, _ = http_request(server.url + path)
            assert status == 404
        for server_name in ["arccrm", "desk", "vault", "hubbench"]:
            status, response = http_request(
                server.url + f"/mcp/{server_name}",
                payload={"jsonrpc": "2.0", "id": 3, "method": "initialize"},
            )
            assert (
                status == 200
                and response["result"]["serverInfo"]["version"] == FAMILY.version
            )
            assert "HubBench" in response["result"]["instructions"]
