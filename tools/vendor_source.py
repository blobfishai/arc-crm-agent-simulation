"""Import a reviewed first-party Git snapshot with exact per-file provenance.

Never reads dirty working-tree bytes, never imports Arc upstream code, and never
replaces an existing differing file. Review changes in a worktree/PR.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PIN = "a40865c97a5f0e0ba39c8e84a98cf1d448546b1f"
ADAPTER = "benchmark/dataset_factory/adapters/arc_crm"
ENGINE_FILES = (
    "__init__.py", "assets.py", "cli.py", "core.sql", "families.py", "http.py",
    "server.py", "tasks.py", "validation.py", "verifier.py", "world.py",
)
EXTRAS = (
    "benchmark/dataset_factory/adapters/__init__.py", "benchmark/hubbench/NOTICE",
    "benchmark/dataset_factory/tests/test_arc_crm.py", "benchmark/dataset_factory/tests/test_arc_crm_surfaces.py",
)


def git(repo: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True).stdout


def import_snapshot(source_repo: Path, revision: str = PIN) -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}", revision) or revision != PIN:
        raise ValueError("only the reviewed full commit pin is accepted")
    if git(source_repo, "rev-parse", f"{revision}^{{commit}}").decode().strip() != revision:
        raise ValueError("source commit identity does not match")
    paths = git(source_repo, "ls-tree", "-r", "--name-only", revision, "--", ADAPTER).decode().splitlines()
    paths += [f"benchmark/hubbench/engine/{name}" for name in ENGINE_FILES] + list(EXTRAS)
    payloads = {}
    records = []
    for name in sorted(paths):
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or not name.startswith("benchmark/"):
            raise ValueError("unsafe source path")
        mode = git(source_repo, "ls-tree", revision, "--", name).decode().split()[0]
        if mode != "100644":
            raise ValueError(f"source member is not a regular nonexecutable file: {name}")
        raw = git(source_repo, "show", f"{revision}:{name}")
        payloads[name] = raw
        records.append({"path": name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
            "git_blob": git(source_repo, "rev-parse", f"{revision}:{name}").decode().strip()})
    manifest = {"schema_version": 1, "source_repository": "https://github.com/blobfishai/blobfishai", "source_commit": revision,
        "method": "byte-identical first-party vendoring; no Arc upstream code or source conversations",
        "files": records}
    payloads["vendor-manifest.json"] = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
    # Check the entire write set before creating any file.
    for name, raw in payloads.items():
        path = ROOT / name
        if any(parent.is_symlink() for parent in (path, *path.parents) if parent.is_relative_to(ROOT)):
            raise ValueError(f"symlinked destination: {name}")
        if path.exists() and (not path.is_file() or path.read_bytes() != raw):
            raise ValueError(f"refusing to overwrite different existing bytes: {name}")
    for name, raw in payloads.items():
        path = ROOT / name
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as handle:
            handle.write(raw)
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-repo", type=Path, required=True)
    args = parser.parse_args()
    result = import_snapshot(args.source_repo)
    print(json.dumps({"source_commit": result["source_commit"], "files": len(result["files"])}, sort_keys=True))
