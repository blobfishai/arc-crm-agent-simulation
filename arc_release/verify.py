"""Trusted post-episode verifier. Invoked as root with Python -I -S."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import urllib.request
from pathlib import Path


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def forbidden(*_):
    raise RuntimeError("verifier metadata cannot execute tools or build tasks")


def family_from_metadata(metadata):
    from benchmark.hubbench.engine.families import Family, ToolSpec

    return Family(
        **metadata["family"],
        schema_sql="",
        tools=tuple(ToolSpec(**item, handler=forbidden) for item in metadata["tools"]),
        build_tasks=forbidden,
    )


def private_credential(control):
    for path in [control, control / "identity.json", control / "verifier-token"]:
        info = path.lstat()
        if path.is_symlink() or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("verifier control path is not private and owned")
    token = (control / "verifier-token").read_text().strip()
    if re.fullmatch(r"[0-9a-f]{64}", token) is None:
        raise ValueError("invalid verifier credential")
    return token


def verify(root: Path, logs: Path, control: Path, url: str, *, bundle=None, isolation=None):
    from benchmark.hubbench.engine.verifier import verify_episode
    from benchmark.hubbench.engine.world import World

    identity = json.loads((root / "identity.json").read_text())
    raw = (root / "task.json").read_bytes()
    if sha(raw) != identity["sealed_task_sha256"]:
        raise ValueError("sealed verifier projection does not match frozen identity")
    collection = None
    if bundle is not None:
        if isolation is None or isolation.is_symlink() or bundle.is_symlink():
            raise ValueError("unsafe or incomplete separate-verifier input")
        members = list(bundle.iterdir())
        if {path.name for path in members} != {"snapshot.json", "world.sqlite"} or any(path.is_symlink() or not path.is_file() for path in members):
            raise ValueError("world bundle membership differs")
        collection = json.loads((bundle / "snapshot.json").read_text())
        snapshot = (bundle / "world.sqlite").read_bytes()
        if collection.get("identity") != identity or collection.get("snapshot_sha256") != sha(snapshot):
            raise ValueError("collected world snapshot identity mismatch")
        if collection.get("collection_uid") != os.getuid() or collection.get("credential_exported") is not False:
            raise ValueError("invalid world-side collection proof")
        if collection.get("method") != "serialized world executor snapshot" or re.fullmatch(r"[0-9a-f]{64}", collection.get("episode_nonce_commitment", "")) is None:
            raise ValueError("invalid snapshot method or episode commitment")
        isolation_proof = json.loads(isolation.read_text())
        if isolation_proof.get("agent_uid") != 10001 or isolation_proof.get("verifier_mode") != "separate":
            raise ValueError("invalid agent isolation proof")
        for field in ["protected_paths_denied", "private_api_denied", "external_tcp_denied"]:
            if isolation_proof.get(field) is not True:
                raise ValueError("agent isolation failed")
        if isolation_proof.get("host_log_mounts_trusted") is not False or isolation_proof.get("world_credentials_mounted_in_agent") is not False:
            raise ValueError("unsafe agent/verifier boundary")
        with (logs / "isolation.json").open("x") as handle:
            handle.write(encoded(isolation_proof).decode() + "\n")
    else:
        # Online mode exists only for local runtime tests, not Harbor grading.
        token = private_credential(control)
        if json.loads((control / "identity.json").read_text()) != identity:
            raise ValueError("world/verifier episode binding mismatch")
        request = urllib.request.Request(url.rstrip("/") + "/verifier/snapshot", headers={"X-Arc-Verifier-Token": token})
        direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with direct.open(request, timeout=20) as response:
            snapshot = response.read(32 * 1024 * 1024 + 1)
            expected_identity = sha(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode())
            if response.headers.get("X-Arc-Identity") != expected_identity or response.headers.get("X-Arc-Snapshot-SHA256") != sha(snapshot):
                raise ValueError("private world snapshot identity mismatch")
    if len(snapshot) > 32 * 1024 * 1024 or not snapshot.startswith(b"SQLite format 3\x00"):
        raise ValueError("invalid or oversized world snapshot")
    database = logs / "world.sqlite"
    with database.open("xb") as handle:
        handle.write(snapshot)
    metadata = json.loads((root / "family.json").read_text())
    with World(family_from_metadata(metadata), json.loads(raw), database) as world:
        if [tuple(row) for row in world.connection.execute("PRAGMA integrity_check")] != [("ok",)]:
            raise ValueError("world snapshot integrity check failed")
        verdict = verify_episode(json.loads(raw), world)
        trace = world.trace
        state = world.snapshot()
    receipt = {
        "identity": identity,
        "snapshot_sha256": sha(snapshot),
        "trace_sha256": sha(encoded(trace)),
        "final_state_sha256": sha(encoded(state)),
        "verdict_sha256": sha(encoded(verdict)),
        "verifier_uid": os.getuid(),
        "world_credential": "random per episode; never exported",
        "mode": "separate" if bundle is not None else "local-online",
        "collection": collection,
    }
    for name, value in [("verdict.json", verdict), ("trace.json", trace), ("receipt.json", receipt)]:
        with (logs / name).open("x") as handle:
            handle.write(encoded(value).decode() + "\n")
    with (logs / "reward.txt").open("x") as handle:
        handle.write(str(verdict["score"] / 100) + "\n")
    return verdict


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/tests"))
    parser.add_argument("--logs", type=Path, default=Path("/logs/verifier"))
    parser.add_argument("--control", type=Path, default=Path("/run/arc-control"))
    parser.add_argument("--url", default="http://world:8766")
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--isolation", type=Path)
    args = parser.parse_args(argv)
    os.umask(0o077)
    sys.path.insert(0, str(args.root / "runtime"))
    args.logs.mkdir(mode=0o700, exist_ok=True)
    if args.bundle is not None:
        # Harbor has stopped/deleted the agent environment and cleared old
        # verifier logs before starting this separate, networkless container.
        if args.logs.is_symlink():
            raise ValueError("symlinked verifier output directory")
        os.chown(args.logs, os.getuid(), os.getgid())
        args.logs.chmod(0o700)
    info = args.logs.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("verifier output directory is not private and owned")
    # These names can never be pre-populated by an agent or reused from a run.
    outputs = ["reward.txt", "reward.json", "world.sqlite", "verdict.json", "trace.json", "receipt.json", "error.json", "isolation.json"]
    if any((args.logs / name).exists() or (args.logs / name).is_symlink() for name in outputs):
        raise ValueError("verifier requires fresh output artifacts")
    try:
        verdict = verify(args.root, args.logs, args.control, args.url, bundle=args.bundle, isolation=args.isolation)
    except Exception as error:
        # No exception strings: network errors can contain credential-bearing
        # request representations. Only a safe class name leaves the verifier.
        with (args.logs / "error.json").open("x") as handle:
            handle.write(json.dumps({"error_type": type(error).__name__, "qualified": False}) + "\n")
        (args.logs / "reward.txt").write_text("0\n")
        return 2
    print(json.dumps({"task_id": verdict["task_id"], "score": verdict["score"], "strict_pass": verdict["strict_pass"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
