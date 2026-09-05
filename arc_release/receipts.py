"""Fail-closed admission of complete, exact, all-six Harbor oracle jobs."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

from .build import DATASET, VERSION, content_hash, json_bytes, sha, verify_freeze
from .verify import encoded


def read_json(path):
    if path.is_symlink():
        raise ValueError(f"symlinked receipt input: {path.name}")
    return json.loads(path.read_text())


def require(value, message):
    if not value:
        raise ValueError(message)


def inspect_snapshot(task, snapshot):
    # Re-grade a disposable copy with the exact frozen verifier closure. Receipt
    # hashes alone cannot prove that a recorded state matches its SQLite bytes.
    script = """
import json, shutil, sys, tempfile
from pathlib import Path
root = Path(sys.argv[1])
sys.path[:0] = [str(root), str(root / 'runtime')]
from verify import family_from_metadata, encoded, sha
from benchmark.hubbench.engine.world import World
from benchmark.hubbench.engine.verifier import verify_episode
task = json.loads((root / 'task.json').read_text())
family = family_from_metadata(json.loads((root / 'family.json').read_text()))
with tempfile.TemporaryDirectory(prefix='arc-regrade-') as temporary:
    target = Path(temporary) / 'world.sqlite'
    shutil.copyfile(sys.argv[2], target)
    with World(family, task, target) as world:
        print(json.dumps({'trace_sha256': sha(encoded(world.trace)), 'final_state_sha256': sha(encoded(world.snapshot())), 'verdict': verify_episode(task, world)}))
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", script, str(task / "tests"), str(snapshot)],
        check=True, capture_output=True, text=True, timeout=20,
    )
    return json.loads(completed.stdout)


def collection_provenance(directory, task):
    entries = read_json(directory / "artifacts/manifest.json")
    expected = [
        {"source": "/export", "destination": "artifacts/arc-world-snapshot", "type": "directory", "status": "ok", "service": "world"},
        {"source": "/run/arc-guard/startup.json", "destination": "artifacts/arc-agent-isolation.json", "type": "file", "status": "ok", "service": "main"},
    ]
    require(isinstance(entries, list) and len(entries) == 3, "unexpected collection manifest membership")
    for entry in expected:
        require(entries.count(entry) == 1, "missing, failed, duplicate or wrong-service collection")
    other = next(entry for entry in entries if entry not in expected)
    require(other.get("source") == "/logs/artifacts" and other.get("destination") == "artifacts/logs/artifacts" and other.get("service") is None and other.get("status") in {"ok", "empty"}, "unexpected conventional artifact collection")
    config = tomllib.loads((task / "task.toml").read_text())
    hooks = config["verifier"]["collect"]
    require(len(hooks) == 1 and hooks[0]["service"] == "world", "expected one frozen world snapshot hook")
    markers = [
        "Stopping main service before sidecar evidence collection", "Main service stopped",
        "Collecting sidecar artifacts from services: ['world']",
        f"Running collect hook in service 'world': {hooks[0]['command']!r}", "Collect hook in service 'world' completed",
    ]
    log = (directory / "trial.log").read_text()
    require(all(log.count(marker) == 1 for marker in markers), "missing/repeated stop or collection success marker")
    require([log.index(marker) for marker in markers] == sorted(log.index(marker) for marker in markers), "world collection preceded agent stop")
    for failure in ["Failed to stop main service", "Collect hook in service 'world' exited with code", "Collect hook in service 'world' failed", "Artifact collision:", "Failed to download artifact", "Agent environment cleanup failed", "Failed to stop verifier env"]:
        require(failure not in log, "stop, collection or cleanup failure was ignored by Harbor")
    return read_json(directory / "artifacts/arc-world-snapshot/snapshot.json")


def admit(frozen: Path, job: Path, *, registry=False, allow_dirty=False):
    frozen, job = Path(frozen).resolve(strict=True), Path(job).resolve(strict=True)
    manifest = verify_freeze(frozen)
    require(allow_dirty or not manifest["source"]["dirty"], "dirty source cannot establish release qualification")
    expected = {reference["name"]: reference for reference in manifest["tasks"]}
    reference_episodes = {item["task_id"]: item["oracle"] for item in read_json(frozen / "qualification.json")["tasks"]}
    lock = read_json(job / "lock.json")
    result = read_json(job / "result.json")
    config = read_json(job / "config.json")
    require(lock.get("schema_version") == 3 and lock.get("harbor", {}).get("version") == "0.21.0", "unsupported Harbor job lock")
    require(lock.get("n_concurrent_trials") == 1 and lock.get("retry", {}).get("max_retries") == 0, "job must be serial with zero retries")
    require(result.get("finished_at") and result.get("n_total_trials") == 6, "job did not finish all six trials")
    stats = result["stats"]
    require(stats.get("n_completed_trials") == 6, "not all six trials completed")
    for key in ["n_errored_trials", "n_running_trials", "n_pending_trials", "n_cancelled_trials", "n_retries"]:
        require(stats.get(key, 0) == 0, f"nonzero job counter: {key}")
    require(config.get("n_attempts", 1) == 1 and config.get("task_names") is None, "filtered or repeated job")
    datasets = config.get("datasets", [])
    require(len(datasets) == 1, "job must contain one exact dataset")
    dataset = datasets[0]
    require(not dataset.get("task_names") and not dataset.get("exclude_task_names") and dataset.get("n_tasks") is None, "dataset subset not admitted")
    if registry:
        require(dataset.get("name") == DATASET and re.fullmatch(r"sha256:[0-9a-f]{64}", dataset.get("ref", "")), "registry dataset must resolve to a digest")
    else:
        require(Path(dataset.get("path", "")).resolve() == frozen / "harbor/tasks", "job dataset path differs from freeze")
    embedded = lock.get("trials", [])
    require(len(embedded) == 6, "job lock must contain exactly six trials")
    trial_dirs = [path for path in job.iterdir() if path.is_dir() and (path / "result.json").is_file()]
    require(len(trial_dirs) == 6, "exactly six trial result directories required")
    seen, nonces, receipts, trial_locks = set(), set(), [], []
    for directory in sorted(trial_dirs):
        trial = read_json(directory / "result.json")
        trial_lock = read_json(directory / "lock.json")
        trial_config = read_json(directory / "config.json")
        name = trial.get("task_name")
        require(name in expected and name not in seen, "unexpected or repeated task result")
        seen.add(name)
        task_id = name.split("/")[1]
        task = frozen / "harbor/tasks" / task_id
        wanted_digest, _ = content_hash(task)
        require(wanted_digest == expected[name]["digest"], "frozen task digest changed")
        require(trial_lock.get("schema_version") == 2, "unsupported trial lock")
        bound = trial_lock["task"]
        require(bound.get("digest") == wanted_digest and bound.get("version") == VERSION, "trial task digest/version differs")
        require(bound.get("name") == (name if registry else task_id), "trial lock name mismatch")
        require(trial.get("source") == (DATASET if registry else "tasks"), "trial source mismatch")
        if registry:
            require(bound.get("type") == "package" and bound.get("source") == DATASET, "trial is not a dataset registry package")
            require(trial.get("task_id") == {"org": "blobfishai", "name": task_id, "ref": wanted_digest}, "registry task reference mismatch")
        else:
            require(bound.get("type") == "local" and Path(bound.get("path", "")).resolve() == task, "local trial path mismatch")
            require(Path(trial.get("task_id", {}).get("path", "")).resolve() == task, "result local path mismatch")
        require(trial_config.get("job_id") == result.get("id") and trial.get("config", {}).get("job_id") == result.get("id"), "trial belongs to a different job")
        require(trial.get("finished_at") and trial.get("exception_info") is None, "unfinished or errored trial")
        for phase in ["environment_setup", "agent_setup", "agent_execution", "verifier"]:
            require(trial.get(phase, {}).get("started_at") and trial.get(phase, {}).get("finished_at"), f"incomplete trial phase: {phase}")
        require(trial.get("agent_info", {}).get("name") == "oracle" and trial.get("agent_info", {}).get("model_info") is None, "not an oracle-only trial")
        require(not trial_lock.get("install_only", False) and trial_lock.get("timeout_multiplier") == 1.0, "noncanonical trial execution")
        agent = trial_lock.get("agent", {})
        require(agent.get("name") == "oracle" and not any(agent.get(key) for key in ["model_name", "import_path", "kwargs", "mcp_servers", "skills", "extra_allowed_hosts", "env", "resume_trajectory", "load_trajectory", "override_timeout_sec"]), "modified oracle configuration")
        environment = trial_lock.get("environment", {})
        require(environment.get("type") == "docker" and environment.get("delete") is True, "not a cleanup-enabled Docker trial")
        require(not any(environment.get(key) for key in ["mounts", "extra_docker_compose", "kwargs", "extra_allowed_hosts"]), "unexpected environment override")
        verifier_config = trial_lock.get("verifier", {})
        require(verifier_config.get("disable") is False and verifier_config.get("environment_mode") == "separate", "noncanonical verifier")
        require(trial.get("verifier_environment_mode") == "separate", "verifier was not isolated from the agent")
        require(not trial.get("config", {}).get("artifacts") and not trial.get("config", {}).get("source_trial"), "runtime artifact or source-trial override")
        collection = collection_provenance(directory, task)
        for artifact in [directory / "agent/exit-code.txt", directory / "agent/exception.txt", directory / "verifier/error.json", directory / "verifier/reward.json"]:
            if artifact.name == "exit-code.txt" and artifact.is_file():
                require(artifact.read_text().strip() == "0", "oracle exited unsuccessfully")
            elif artifact.exists() or artifact.is_symlink():
                raise ValueError(f"failure or competing reward artifact: {artifact.name}")
        verifier = directory / "verifier"
        verdict = read_json(verifier / "verdict.json")
        trace = read_json(verifier / "trace.json")
        proof = read_json(verifier / "receipt.json")
        isolation = read_json(verifier / "isolation.json")
        surface = read_json(directory / "agent/surfaces.json")
        oracle = reference_episodes[task_id]
        require(verdict == oracle["verdict"] and verdict.get("strict_pass") is True and verdict.get("score") == 100, "strict canonical verdict required")
        require(trace == oracle["trace"] and all(entry.get("success") is True for entry in trace), "noncanonical or failed tool trace")
        require(proof.get("identity") == read_json(task / "tests/identity.json") and proof.get("verifier_uid") == 0, "trusted verifier identity mismatch")
        require(proof.get("mode") == "separate" and proof.get("collection") == collection, "collection receipt differs from world-side artifact")
        require(collection.get("identity") == proof["identity"] and collection.get("collection_uid") == 0 and collection.get("credential_exported") is False, "world-side snapshot identity or authority differs")
        nonce = collection.get("episode_nonce_commitment", "")
        require(re.fullmatch(r"[0-9a-f]{64}", nonce) and nonce not in nonces, "episode credential commitment missing or reused")
        nonces.add(nonce)
        require(proof.get("trace_sha256") == sha(encoded(trace)) and proof.get("verdict_sha256") == sha(encoded(verdict)), "trace/verdict receipt hash mismatch")
        require(proof.get("final_state_sha256") == sha(encoded(oracle["final_state"])), "final state differs from canonical episode")
        require(proof.get("snapshot_sha256") == sha((verifier / "world.sqlite").read_bytes()), "snapshot receipt hash mismatch")
        require(collection.get("snapshot_sha256") == proof["snapshot_sha256"] == sha((directory / "artifacts/arc-world-snapshot/world.sqlite").read_bytes()), "collected and graded snapshots differ")
        reconstructed = inspect_snapshot(task, verifier / "world.sqlite")
        require(reconstructed["verdict"] == verdict and reconstructed["trace_sha256"] == proof["trace_sha256"] and reconstructed["final_state_sha256"] == proof["final_state_sha256"], "snapshot does not reproduce the recorded verdict/trace/state")
        require(isolation == read_json(directory / "artifacts/arc-agent-isolation.json"), "isolation proof differs from protected main artifact")
        require(isolation.get("agent_uid") == 10001 and all(isolation.get(key) is True for key in ["protected_paths_denied", "private_api_denied", "external_tcp_denied"]), "missing dropped-UID isolation proof")
        require(isolation.get("verifier_mode") == "separate" and isolation.get("host_log_mounts_trusted") is False and isolation.get("world_credentials_mounted_in_agent") is False, "unsafe verifier isolation strategy")
        require(isolation.get("main_limits") == {"cpu.max": "100000 100000", "memory.max": "1073741824", "pids.max": "256"}, "main resource enforcement proof missing")
        require(isolation.get("world_limits") == {"cpu.max": "50000 100000", "memory.max": "536870912", "pids.max": "128"}, "world resource enforcement proof missing")
        require(surface.get("uid") == 10001 and surface.get("tool_errors") == 0, "oracle account or tool errors invalid")
        events = surface.get("events", [])
        require(len(events) == len(trace) and {entry["surface"] for entry in events} == {"cli", "web", "rest", "mcp"}, "incomplete four-surface oracle")
        require("web" in surface.get("write_surfaces", []), "no actual web mutation")
        for index, (event, entry) in enumerate(zip(events, trace, strict=True)):
            require(event["index"] == index and event["tool"] == entry["tool"] and event["result_sha256"] == sha(encoded(entry["result"])), "surface event differs from durable trace")
        require((verifier / "reward.txt").read_text().strip() in {"1", "1.0"} and trial.get("verifier_result", {}).get("rewards") == {"reward": 1.0}, "Harbor reward differs from strict verdict")
        hashes = {}
        for relative in ["lock.json", "config.json", "result.json", "trial.log", "artifacts/manifest.json", "artifacts/arc-world-snapshot/snapshot.json", "artifacts/arc-world-snapshot/world.sqlite", "artifacts/arc-agent-isolation.json", "agent/surfaces.json", "verifier/receipt.json", "verifier/isolation.json", "verifier/verdict.json", "verifier/trace.json", "verifier/world.sqlite", "verifier/reward.txt"]:
            hashes[relative] = sha((directory / relative).read_bytes())
        receipts.append({"task": name, "task_digest": wanted_digest, "trial": directory.name, "artifact_sha256": hashes})
        trial_locks.append(trial_lock)
    require(seen == set(expected), "missing tasks")
    require(sorted(map(encoded, embedded)) == sorted(map(encoded, trial_locks)), "job lock does not match trial locks")
    return {
        "schema_version": 1, "dataset": DATASET, "version": VERSION, "qualified": True,
        "scope": "all-six registry Docker oracles" if registry else "all-six local Docker oracles",
        "release_qualification": not manifest["source"]["dirty"], "model_evaluated": False,
        "source_commit": manifest["source"]["commit"], "source_dirty": manifest["source"]["dirty"],
        "manifest_sha256": sha((frozen / "manifest.json").read_bytes()), "harbor_version": "0.21.0",
        "job_id": result["id"], "job": str(job), "job_lock_sha256": sha((job / "lock.json").read_bytes()),
        "dataset_reference": dataset, "task_count": 6, "strict_passes": 6,
        "trials": sorted(receipts, key=lambda item: item["task"]),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("frozen", type=Path)
    parser.add_argument("job", type=Path)
    parser.add_argument("--registry", action="store_true")
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    receipt = admit(args.frozen, args.job, registry=args.registry, allow_dirty=args.allow_dirty)
    if args.output.resolve().is_relative_to(args.frozen.resolve()):
        parser.error("receipts must not mutate the frozen package")
    with args.output.open("xb") as handle:
        handle.write(json_bytes(receipt))
    print(json.dumps({key: receipt[key] for key in ["qualified", "scope", "release_qualification", "task_count", "strict_passes"]}))


if __name__ == "__main__":
    main()
