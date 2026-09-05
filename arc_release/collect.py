"""Trusted world-side collection hook; no task builders or grading imports."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import urllib.request
from pathlib import Path


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def collect(root, control, output, url):
    os.umask(0o077)
    identity = json.loads((root / "identity.json").read_text())
    for path in [control, control / "verifier-token", control / "identity.json"]:
        info = path.lstat()
        if path.is_symlink() or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("world collection credentials are not private")
    if json.loads((control / "identity.json").read_text()) != identity:
        raise ValueError("world collection binding mismatch")
    token = (control / "verifier-token").read_text().strip()
    if re.fullmatch(r"[0-9a-f]{64}", token) is None:
        raise ValueError("invalid collection credential")
    direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url.rstrip("/") + "/verifier/snapshot", headers={"X-Arc-Verifier-Token": token})
    with direct.open(request, timeout=20) as response:
        raw = response.read(32 * 1024 * 1024 + 1)
        if len(raw) > 32 * 1024 * 1024 or not raw.startswith(b"SQLite format 3\x00"):
            raise ValueError("invalid collected snapshot")
        expected_identity = sha(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode())
        if response.headers.get("X-Arc-Identity") != expected_identity or response.headers.get("X-Arc-Snapshot-SHA256") != sha(raw):
            raise ValueError("snapshot collection identity mismatch")
    if output.is_symlink():
        raise ValueError("collection destination must not be a symlink")
    output.mkdir(mode=0o700, exist_ok=False)
    with (output / "world.sqlite").open("xb") as handle:
        handle.write(raw)
    proof = {"identity": identity, "snapshot_sha256": sha(raw), "collection_uid": os.getuid(),
             "method": "serialized world executor snapshot", "credential_exported": False,
             "episode_nonce_commitment": sha(token.encode())}
    with (output / "snapshot.json").open("x") as handle:
        json.dump(proof, handle, sort_keys=True)
    return proof


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/opt/arc-world"))
    parser.add_argument("--control", type=Path, default=Path("/run/arc-control"))
    parser.add_argument("--output", type=Path, default=Path("/export"))
    parser.add_argument("--url", default="http://127.0.0.1:8766")
    args = parser.parse_args()
    proof = collect(args.root, args.control, args.output, args.url)
    print(json.dumps({"task_id": proof["identity"]["task_id"], "snapshot_sha256": proof["snapshot_sha256"]}))


if __name__ == "__main__":
    main()
