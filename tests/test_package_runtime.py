import json
import os
import select
import subprocess
import sys
import urllib.error
import urllib.request
from contextlib import contextmanager

import pytest

from arc_release.build import freeze
from arc_release.collect import collect
from arc_release.verify import encoded, sha
from arc_release.world import prepare_episode
from benchmark.dataset_factory.adapters.arc_crm import FAMILY, build_tasks


@pytest.fixture(scope="module")
def runtime_freeze(tmp_path_factory):
    root = tmp_path_factory.mktemp("arc-runtime") / "freeze"
    freeze(root)
    return root


@contextmanager
def launched(task, temporary):
    root = task / "environment/world"
    control = temporary / "control"
    process = subprocess.Popen(
        [sys.executable, "-I", "-S", "-B", str(root / "world.py"), "--root", str(root), "--state", str(temporary / "state"),
         "--control", str(control), "--host", "127.0.0.1", "--port", "0", "--private-port", "0"],
        cwd=temporary, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert select.select([process.stdout], [], [], 15)[0], "world failed to announce startup"
        line = process.stdout.readline()
        assert line, process.stderr.read()
        status = json.loads(line)
        yield status["public_url"], f"http://127.0.0.1:{status['private_port']}", control
    finally:
        process.terminate()
        try:
            _, errors = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            _, errors = process.communicate(timeout=5)
            pytest.fail("world failed to stop")
        assert process.returncode == 0, errors


def verify_process(task, private, control, logs, cwd):
    return subprocess.run(
        [sys.executable, "-I", "-S", "-B", str(task / "tests/verify.py"), "--root", str(task / "tests"),
         "--logs", str(logs), "--control", str(control), "--url", private],
        cwd=cwd, capture_output=True, text=True, timeout=30,
    )


@pytest.mark.parametrize("number", range(1, 7))
def test_isolated_runtime_closures_share_four_surfaces_and_grade(runtime_freeze, number, tmp_path):
    task_id = f"arc-crm-{number:03}"
    task = runtime_freeze / "harbor/tasks" / task_id
    report = json.loads((runtime_freeze / "qualification.json").read_text())
    expected = report["tasks"][number - 1]["oracle"]
    logs = tmp_path / "verifier"
    with launched(task, tmp_path) as (url, private, control):
        # Private health and denied requests must not create evidence in trace.
        for token in [None, "wrong"]:
            request = urllib.request.Request(private + "/verifier/snapshot", headers={"X-Arc-Verifier-Token": token} if token else {})
            with pytest.raises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request, timeout=5)
            assert raised.value.code == 403
            raised.value.close()
        for method in ["POST", "PUT", "PATCH", "DELETE"]:
            with pytest.raises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(urllib.request.Request(private + "/verifier/snapshot", method=method), timeout=5)
            assert raised.value.code == 405
            raised.value.close()
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-B", str(task / "solution/oracle.py"), "--root", str(task / "solution"),
             "--client", str(task / "environment/client/client.py"), "--log", str(tmp_path / "surfaces.json"),
             "--url", url, "--require-uid", str(os.getuid())],
            cwd=tmp_path, capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, result.stderr
        surfaces = json.loads((tmp_path / "surfaces.json").read_text())
        assert {entry["surface"] for entry in surfaces["events"]} == {"cli", "web", "rest", "mcp"}
        assert "web" in surfaces["write_surfaces"] and surfaces["tool_errors"] == 0
        result = verify_process(task, private, control, logs, tmp_path)
        assert result.returncode == 0, result.stderr
        verdict = json.loads((logs / "verdict.json").read_text())
        assert verdict == expected["verdict"] and verdict["strict_pass"] and verdict["score"] == 100
        assert json.loads((logs / "trace.json").read_text()) == expected["trace"]
        receipt = json.loads((logs / "receipt.json").read_text())
        assert receipt["final_state_sha256"] == sha(encoded(expected["final_state"]))
        assert receipt["trace_sha256"] == sha(encoded(expected["trace"]))
        assert (logs / "reward.txt").read_text().strip() == "1.0"
        bundle = tmp_path / "export"
        collect(task / "environment/world", control, bundle, private)
        isolation = tmp_path / "isolation.json"
        isolation.write_text(json.dumps({
            "agent_uid": 10001, "verifier_mode": "separate", "protected_paths_denied": True,
            "private_api_denied": True, "external_tcp_denied": True,
            "host_log_mounts_trusted": False, "world_credentials_mounted_in_agent": False,
        }))
        offline = tmp_path / "offline-verifier"
        command = [sys.executable, "-I", "-S", "-B", str(task / "tests/verify.py"), "--root", str(task / "tests"),
                   "--logs", str(offline), "--bundle", str(bundle), "--isolation", str(isolation)]
        completed = subprocess.run(command, capture_output=True, text=True, timeout=20)
        assert completed.returncode == 0, completed.stderr
        assert json.loads((offline / "verdict.json").read_text()) == verdict
        assert json.loads((offline / "receipt.json").read_text())["mode"] == "separate"
        with pytest.raises(FileExistsError):
            collect(task / "environment/world", control, bundle, private)
        token = (control / "verifier-token").read_text().strip()
        assert all(token not in path.read_bytes().decode(errors="replace") for path in logs.iterdir() if path.is_file())
        # A second verifier cannot overwrite a receipt/reward from an episode.
        assert verify_process(task, private, control, logs, tmp_path).returncode != 0
        (bundle / "extra.txt").write_text("unlisted bundle input")
        command[command.index(str(offline))] = str(tmp_path / "bad-bundle")
        completed = subprocess.run(command, capture_output=True, text=True, timeout=20)
        assert completed.returncode == 2
        assert (tmp_path / "bad-bundle/reward.txt").read_text().strip() == "0"


def test_noop_is_not_strict_and_oracle_errors_fail(runtime_freeze, tmp_path):
    task = runtime_freeze / "harbor/tasks/arc-crm-001"
    with launched(task, tmp_path) as (url, private, control):
        client = subprocess.run(
            [sys.executable, "-I", "-S", "-B", str(task / "environment/client/client.py"), "--url", url, "not.a.tool", "{}"],
            capture_output=True, text=True, timeout=10,
        )
        assert client.returncode != 0
        logs = tmp_path / "noop"
        result = verify_process(task, private, control, logs, tmp_path)
        assert result.returncode == 0, result.stderr
        assert not json.loads((logs / "verdict.json").read_text())["strict_pass"]
        assert float((logs / "reward.txt").read_text()) < 1
        solution = tmp_path / "invalid-solution"
        solution.mkdir()
        (solution / "steps.json").write_text(json.dumps([{"tool": "not.a.tool", "arguments": {}}]))
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-B", str(task / "solution/oracle.py"), "--root", str(solution), "--url", url,
             "--client", str(task / "environment/client/client.py"), "--require-uid", str(os.getuid()), "--log", str(tmp_path / "bad.json")],
            capture_output=True, text=True, timeout=15,
        )
        assert result.returncode != 0
        assert not (tmp_path / "bad.json").exists()


def test_verifier_transport_failure_is_zero_nonzero_exit(runtime_freeze, tmp_path):
    task = runtime_freeze / "harbor/tasks/arc-crm-001"
    logs = tmp_path / "failed"
    result = verify_process(task, "http://127.0.0.1:1", tmp_path / "no-control", logs, tmp_path)
    assert result.returncode == 2
    assert (logs / "reward.txt").read_text().strip() == "0"
    assert (logs / "error.json").is_file()
    assert not (logs / "verdict.json").exists()


def test_session_is_random_persistent_and_never_silently_reset(tmp_path):
    task = build_tasks()[0]
    identity = {"task_id": task["task_id"], "contract": "test"}
    state, control = tmp_path / "state", tmp_path / "control"
    database, first = prepare_episode(state, control, identity, FAMILY, task)
    before = database.read_bytes()
    assert prepare_episode(state, control, identity, FAMILY, task) == (database, first)
    _, second = prepare_episode(tmp_path / "state2", tmp_path / "control2", identity, FAMILY, task)
    assert first != second
    with pytest.raises(ValueError, match="binding"):
        prepare_episode(state, control, identity | {"contract": "different"}, FAMILY, task)
    assert database.read_bytes() == before
    linked = tmp_path / "symlink-state"
    linked.symlink_to(state, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        prepare_episode(linked, control, identity, FAMILY, task)
    (control / "unowned").write_text("unexpected")
    with pytest.raises(ValueError, match="unexpected"):
        prepare_episode(state, control, identity, FAMILY, task)
