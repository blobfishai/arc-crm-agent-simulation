import json
import shutil

import pytest

from arc_release.build import VERSION, freeze, json_bytes, sha
from arc_release.receipts import admit
from arc_release.verify import encoded
from benchmark.dataset_factory.adapters.arc_crm import FAMILY, build_tasks
from benchmark.dataset_factory.adapters.arc_crm.qualification import run


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json_bytes(value))


@pytest.fixture(scope="module")
def saved_fixture(tmp_path_factory):
    root = tmp_path_factory.mktemp("arc-receipts")
    frozen, job = root / "frozen", root / "job"
    manifest = freeze(frozen)
    tasks = build_tasks()
    locks = []
    for task, reference in zip(tasks, manifest["tasks"], strict=True):
        task_id = task["task_id"]
        package = frozen / "harbor/tasks" / task_id
        directory = job / (task_id + "__fixture")
        verifier = directory / "verifier"
        verifier.mkdir(parents=True)
        episode = run(task, task["oracle_steps"], verifier / "world.sqlite")
        identity = json.loads((package / "tests/identity.json").read_text())
        write(verifier / "trace.json", episode["trace"])
        write(verifier / "verdict.json", episode["verdict"])
        collection = {"identity": identity, "snapshot_sha256": sha((verifier / "world.sqlite").read_bytes()),
                      "collection_uid": 0, "credential_exported": False, "method": "serialized world executor snapshot",
                      "episode_nonce_commitment": sha(task_id.encode())}
        write(verifier / "receipt.json", {
            "identity": identity, "verifier_uid": 0,
            "mode": "separate", "collection": collection,
            "trace_sha256": sha(encoded(episode["trace"])), "verdict_sha256": sha(encoded(episode["verdict"])),
            "final_state_sha256": sha(encoded(episode["final_state"])), "snapshot_sha256": sha((verifier / "world.sqlite").read_bytes()),
        })
        write(verifier / "isolation.json", {
            "agent_uid": 10001, "protected_paths_denied": True, "private_api_denied": True, "external_tcp_denied": True,
            "verifier_mode": "separate", "host_log_mounts_trusted": False, "world_credentials_mounted_in_agent": False,
            "main_limits": {"cpu.max": "100000 100000", "memory.max": "1073741824", "pids.max": "256"},
            "world_limits": {"cpu.max": "50000 100000", "memory.max": "536870912", "pids.max": "128"},
        })
        write(directory / "artifacts/arc-world-snapshot/snapshot.json", collection)
        shutil.copyfile(verifier / "world.sqlite", directory / "artifacts/arc-world-snapshot/world.sqlite")
        shutil.copyfile(verifier / "isolation.json", directory / "artifacts/arc-agent-isolation.json")
        write(directory / "artifacts/manifest.json", [
            {"source": "/export", "destination": "artifacts/arc-world-snapshot", "type": "directory", "status": "ok", "service": "world"},
            {"source": "/run/arc-guard/startup.json", "destination": "artifacts/arc-agent-isolation.json", "type": "file", "status": "ok", "service": "main"},
            {"source": "/logs/artifacts", "destination": "artifacts/logs/artifacts", "type": "directory", "status": "empty", "service": None},
        ])
        (directory / "trial.log").write_text("\n".join([
            "Stopping main service before sidecar evidence collection", "Main service stopped",
            "Collecting sidecar artifacts from services: ['world']",
            "Running collect hook in service 'world': '/usr/local/bin/python -I -S -B /opt/arc-world/collect.py'",
            "Collect hook in service 'world' completed",
        ]))
        (verifier / "reward.txt").write_text("1.0\n")
        events, writes = [], []
        for index, entry in enumerate(episode["trace"]):
            surface = ("rest", "cli", "mcp", "web")[index % 4]
            if entry["tool"] in FAMILY.write_tools - {"hubbench.submit_answer"}:
                surface = ("web", "mcp", "cli")[len(writes) % 3]
                writes.append(surface)
            if entry["tool"] == "hubbench.context.get":
                surface = "rest"
            events.append({"index": index, "tool": entry["tool"], "surface": surface, "result_sha256": sha(encoded(entry["result"]))})
        write(directory / "agent/surfaces.json", {"uid": 10001, "tool_errors": 0, "write_surfaces": writes, "events": events})
        lock = {
            "schema_version": 2,
            "task": {"name": task_id, "version": VERSION, "type": "local", "digest": reference["digest"], "source": "tasks", "path": str(package)},
            "agent": {"name": "oracle", "kwargs": {}},
            "environment": {"type": "docker", "delete": True},
            "verifier": {"disable": False, "environment_mode": "separate"},
            "install_only": False, "timeout_multiplier": 1.0,
        }
        locks.append(lock)
        write(directory / "lock.json", lock)
        write(directory / "config.json", {"job_id": "unit-test-job"})
        write(directory / "result.json", {
            "task_name": reference["name"], "source": "tasks", "task_id": {"path": str(package)},
            "config": {"job_id": "unit-test-job"}, "finished_at": "2026-09-05T00:00:01Z", "exception_info": None,
            "agent_info": {"name": "oracle", "model_info": None}, "verifier_environment_mode": "separate",
            "verifier_result": {"rewards": {"reward": 1.0}},
            **{phase: {"started_at": "2026-09-05T00:00:00Z", "finished_at": "2026-09-05T00:00:01Z"}
               for phase in ["environment_setup", "agent_setup", "agent_execution", "verifier"]},
        })
    write(job / "lock.json", {"schema_version": 3, "harbor": {"version": "0.21.0"}, "n_concurrent_trials": 1,
                              "retry": {"max_retries": 0}, "trials": locks})
    write(job / "result.json", {"id": "unit-test-job", "finished_at": "2026-09-05T00:00:02", "n_total_trials": 6,
                                "stats": {"n_completed_trials": 6, "n_errored_trials": 0}})
    write(job / "config.json", {"datasets": [{"path": str(frozen / "harbor/tasks")}], "n_concurrent_trials": 1})
    return frozen, job


@pytest.fixture
def candidate(saved_fixture, tmp_path):
    frozen, original = saved_fixture
    job = tmp_path / "job"
    shutil.copytree(original, job)
    return frozen, job


def test_complete_canonical_fixture_is_admitted_but_not_a_release(candidate):
    frozen, job = candidate
    receipt = admit(frozen, job, allow_dirty=True)
    assert receipt["qualified"] and receipt["strict_passes"] == 6
    assert receipt["model_evaluated"] is False
    if json.loads((frozen / "manifest.json").read_text())["source"]["dirty"]:
        assert receipt["release_qualification"] is False
        with pytest.raises(ValueError, match="dirty"):
            admit(frozen, job)


@pytest.mark.parametrize(("relative", "keys", "value"), [
    ("result.json", ["stats", "n_completed_trials"], 5),
    ("result.json", ["stats", "n_errored_trials"], 1),
    ("result.json", ["finished_at"], None),
    ("lock.json", ["retry", "max_retries"], 1),
    ("lock.json", ["n_concurrent_trials"], 2),
    ("config.json", ["datasets", 0, "task_names"], ["arc-crm-001"]),
    ("arc-crm-001__fixture/lock.json", ["task", "digest"], "sha256:" + "0" * 64),
    ("arc-crm-001__fixture/lock.json", ["task", "version"], "0.0.0"),
    ("arc-crm-001__fixture/lock.json", ["environment", "delete"], False),
    ("arc-crm-001__fixture/lock.json", ["environment", "mounts"], ["extra"]),
    ("arc-crm-001__fixture/lock.json", ["agent", "kwargs"], {"custom": True}),
    ("arc-crm-001__fixture/lock.json", ["verifier", "disable"], True),
    ("arc-crm-001__fixture/result.json", ["exception_info"], {"exception_type": "Failure"}),
    ("arc-crm-001__fixture/result.json", ["agent_info", "model_info"], {"name": "not-oracle"}),
    ("arc-crm-001__fixture/result.json", ["verifier_result", "rewards", "reward"], 0.99),
    ("arc-crm-001__fixture/verifier/verdict.json", ["strict_pass"], False),
    ("arc-crm-001__fixture/verifier/receipt.json", ["verifier_uid"], 10001),
    ("arc-crm-001__fixture/verifier/receipt.json", ["final_state_sha256"], "0" * 64),
    ("arc-crm-001__fixture/verifier/isolation.json", ["protected_paths_denied"], False),
    ("arc-crm-001__fixture/agent/surfaces.json", ["tool_errors"], 1),
    ("arc-crm-001__fixture/agent/surfaces.json", ["uid"], 0),
    ("arc-crm-001__fixture/agent/surfaces.json", ["events"], []),
    ("arc-crm-001__fixture/artifacts/manifest.json", [0, "service"], "main"),
    ("arc-crm-001__fixture/artifacts/manifest.json", [0, "status"], "failed"),
])
def test_rejects_false_success_and_identity_or_isolation_drift(candidate, relative, keys, value):
    frozen, job = candidate
    path = job / relative
    document = json.loads(path.read_text())
    target = document
    for key in keys[:-1]:
        target = target[key]
    target[keys[-1]] = value
    write(path, document)
    with pytest.raises(ValueError):
        admit(frozen, job, allow_dirty=True)


@pytest.mark.parametrize("relative", ["agent/exit-code.txt", "verifier/reward.json", "verifier/error.json"])
def test_oracle_failure_or_competing_reward_rejected(candidate, relative):
    frozen, job = candidate
    (job / "arc-crm-001__fixture" / relative).write_text("1")
    with pytest.raises(ValueError):
        admit(frozen, job, allow_dirty=True)


def test_even_rehashed_wrong_snapshot_is_regraded_and_rejected(candidate):
    import sqlite3

    frozen, job = candidate
    verifier = job / "arc-crm-001__fixture/verifier"
    with sqlite3.connect(verifier / "world.sqlite") as connection:
        connection.execute("DELETE FROM answers")
    receipt = json.loads((verifier / "receipt.json").read_text())
    receipt["snapshot_sha256"] = sha((verifier / "world.sqlite").read_bytes())
    receipt["collection"]["snapshot_sha256"] = receipt["snapshot_sha256"]
    write(verifier.parent / "artifacts/arc-world-snapshot/snapshot.json", receipt["collection"])
    shutil.copyfile(verifier / "world.sqlite", verifier.parent / "artifacts/arc-world-snapshot/world.sqlite")
    write(verifier / "receipt.json", receipt)
    with pytest.raises(ValueError, match="snapshot does not reproduce"):
        admit(frozen, job, allow_dirty=True)


def test_job_lock_must_match_every_trial_lock(candidate):
    frozen, job = candidate
    path = job / "lock.json"
    lock = json.loads(path.read_text())
    lock["trials"][0]["timeout_multiplier"] = 2
    write(path, lock)
    with pytest.raises(ValueError, match="job lock"):
        admit(frozen, job, allow_dirty=True)


@pytest.mark.parametrize("change", ["missing-stop", "failed-collect", "wrong-order", "duplicate-manifest"])
def test_best_effort_collection_is_not_an_admission_shortcut(candidate, change):
    frozen, job = candidate
    directory = job / "arc-crm-001__fixture"
    path = directory / "trial.log"
    text = path.read_text()
    if change == "missing-stop":
        path.write_text(text.replace("Main service stopped", "Main service stop omitted"))
    elif change == "failed-collect":
        path.write_text(text + "\nCollect hook in service 'world' failed\n")
    elif change == "wrong-order":
        path.write_text("\n".join(reversed(text.splitlines())))
    else:
        path = directory / "artifacts/manifest.json"
        manifest = json.loads(path.read_text())
        write(path, manifest + [manifest[0]])
    with pytest.raises(ValueError):
        admit(frozen, job, allow_dirty=True)
