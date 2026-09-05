"""Build new-only publication wrappers without changing a qualified task byte."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import tomllib
from pathlib import Path

from arc_release.build import DATASET, VERSION, json_bytes, safe_relative, sha, verify_freeze
from arc_release.receipts import admit, read_json, require

HF_REPO = "SamuelChien821/arc-crm-6"
TAG = "v" + VERSION


def write(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(raw.encode() if isinstance(raw, str) else raw)


def public_receipt(receipt):
    """Exclude workstation paths; retain exact artifact digests and job identity."""
    return {key: value for key, value in receipt.items() if key not in {"job", "dataset_reference"}} | {
        "original_receipt_sha256": sha(json_bytes(receipt)),
        "dataset_reference": {key: value for key, value in receipt["dataset_reference"].items() if key != "path"},
        "workstation_paths_omitted": True,
    }


def inventory(root):
    root = Path(root).resolve(strict=True)
    entries = []
    for path in sorted(root.rglob("*")):
        require(not path.is_symlink(), "symlink in publication")
        if path.is_file():
            raw = path.read_bytes()
            entries.append({"path": path.relative_to(root).as_posix(), "bytes": len(raw), "sha256": sha(raw)})
    return entries


def seal(root):
    write(root / "publication-manifest.json", json_bytes({"schema_version": 1, "files": inventory(root)}))


def verify_bundle(root):
    root = Path(root).resolve(strict=True)
    require(not (root / "publication-manifest.json").is_symlink(), "symlinked publication manifest")
    manifest = read_json(root / "publication-manifest.json")
    actual = [entry for entry in inventory(root) if entry["path"] != "publication-manifest.json"]
    require(actual == manifest["files"], "publication membership or bytes changed")
    return manifest


def digest_dataset(manifest):
    base = ",".join(sorted(task["digest"].removeprefix("sha256:") for task in manifest["tasks"]))
    if manifest.get("files"):
        base += ";" + ",".join(sorted(f"{file['path']}:{file['digest'].removeprefix('sha256:')}" for file in manifest["files"]))
    return "sha256:" + sha(base.encode())


def card(source, *, registry_digest=None):
    registry = (
        f"All six packages also passed a complete, digest-pinned Harbor registry round trip.\n\n"
        f"```sh\nharbor run -d {DATASET}@{registry_digest} -a oracle -e docker -n 1 -k 1 -r 0\n```\n"
        if registry_digest else
        "This dataset binds the locally qualified packages. Registry execution and remote-object\n"
        "receipts are separate evidence; this publication-time card does not claim those gates.\n\n"
        f"```sh\nharbor run -d {DATASET}@{TAG} -a oracle -e docker -n 1 -k 1 -r 0\n```\n"
    )
    return f"""# Blobfish Arc CRM 6 · {VERSION}

Six independently authored synthetic CRM workflows with 31 domain/evidence tools
on three mock servers, two engine controls, and 33 synthetic PDF/XLSX/EML assets.
Each episode shares one stateful world across CLI, real HTML forms, REST and MCP.

## Scope and results

All six frozen reference solutions pass local Docker execution with strict score
100 and exact final-state/trace parity. All 167 authoring negative controls reject.
These are **oracle solvability checks, not model leaderboard results**. No model
is ranked. Independent qualification admits the complete task set, not a sample.

The inspiration, `Arc-Intelligence/arc-crm-benchmark`, has 1,200 conversations and
27 observed tool names at its source pin. We inspected those rows but reproduce
**zero source rows**. Six new workflows map 16 observed names behind 15 independent
contracts; this is partial source-inspired coverage, not upstream API/ABI parity,
and not adaptation of every Harbor or Hugging Face catalog record.

{registry}
## Explore and reproduce

- [Clean implementation]({source['repository']}/tree/{source['commit']})
- [Hugging Face dataset](https://huggingface.co/datasets/{HF_REPO})
- [Harbor dataset](https://hub.harborframework.com/datasets/{DATASET}/{TAG})
- `release.json`: qualified freeze identity and exact task digests.
- `source-lock.json`: upstream pin, inspected fields, license and coverage limits.
- `qualification.json`: authoring traces, replays and negative controls.
- `local-oracle-receipt.json`: all-six local Docker artifact hashes; workstation
  paths omitted, original receipt digest retained.

The Hugging Face distribution includes `frozen/` (the complete unmodified input
freeze, including its original candidate manifest), `data/tasks.jsonl` (six flat
preview rows), `evidence/` (collected snapshots and oracle traces) and separate
registry receipts. The frozen candidate labels are immutable historical inputs;
the external publication receipts describe the later qualification stages.

Asset `file_sha256` / `file_bytes` bind the native PDF/XLSX/EML download.
`content_sha256` and legacy `sha256` instead bind the full UTF-8 evidence text.
Native PDFs wrap and paginate without clipping; independent extraction tests
cover every PDF, and frozen admission checks complete-text rendering.
Version 0.1.1 passed runtime oracles but truncated nine PDF files; its immutable
commit and receipts remain historical evidence, not this corrected release.

## Isolation and grading

Harbor 0.21.0 and Docker are required. The non-root UID10001 agent has only public
client/schema/evidence. Its image has no seed state, builder, oracle or verifier.
An internal network connects main (1 CPU/1 GiB/pids256) to world
(0.5 CPU/512 MiB/pids128). Zero GPUs or paid model calls are needed for oracles.
The random private snapshot credential exists only in the world container.

Harbor stops main, collects the world snapshot, destroys the agent environment,
then starts a fresh networkless verifier (1 CPU/1 GiB/pids256). Agent-writable log
files are not trusted. State-diff, required reads/writes, protected rows and table
counts are graded deterministically by HubScore; there is no LLM judge. Harbor
reward is 1 only for strict pass. Recorded scores are diagnostic, not model ranks.

Build timeout: 600s; agent: 1200s; collection: 40s; verifier: 120s. The declared
2 GiB storage budget is metadata, not a Docker-enforced disk quota. Docker cleanup
removes trial containers/networks/volumes, not the downloaded dataset or evidence.
Do not mount the complete package into an agent. `solution/` is oracle-only and
`tests/` belongs only in the separate grading container.

All organizations and evidence are synthetic. Code/fixtures are Apache-2.0;
the upstream card's MIT label is source metadata, not a license assertion for its
linked code. HubBench names in the shared engine are attribution, not membership
in the separate HubBench benchmark. See LICENSE and ENGINE-NOTICE in `frozen/`.
"""


def prepare_harbor(frozen, local_job, output):
    frozen, output = Path(frozen).resolve(strict=True), Path(output).resolve()
    manifest = verify_freeze(frozen)
    require(manifest["version"] == VERSION, "publication builder version differs from freeze")
    receipt = admit(frozen, local_job)
    require(not output.exists() and not output.is_relative_to(frozen), "new external publication directory required")
    output.mkdir(parents=True)
    release = {
        "schema_version": 1, "dataset": DATASET, "version": VERSION,
        "source": manifest["source"], "frozen_manifest_sha256": sha((frozen / "manifest.json").read_bytes()),
        "tasks": manifest["tasks"], "task_count": 6, "source_rows_reproduced": 0,
        "local_docker_strict_passes": 6, "model_evaluated": False,
        "qualification_scope": "all-six local Docker oracles; later registry/HF gates have separate receipts",
    }
    write(output / "release.json", json_bytes(release))
    write(output / "README.md", card(manifest["source"]))
    write(output / "local-oracle-receipt.json", json_bytes(public_receipt(receipt)))
    for name in ["source-lock.json", "qualification.json"]:
        write(output / name, (frozen / name).read_bytes())
    dataset = (frozen / "harbor/dataset.toml").read_text()
    for entry in inventory(output):
        dataset += f'\n[[files]]\npath = "{entry["path"]}"\ndigest = "sha256:{entry["sha256"]}"\n'
    write(output / "dataset.toml", dataset)
    seal(output)
    verify_freeze(frozen)
    return {"dataset": DATASET, "digest": digest_dataset(tomllib.loads(dataset)), "tasks": 6}


def copy_evidence(frozen, job, receipt, output):
    # Explicit allowlist: no host config/env files, private credentials or agent
    # workspace copies. Every exported byte is covered by an admitted receipt.
    allowed = {
        "agent/surfaces.json", "verifier/receipt.json", "verifier/isolation.json",
        "verifier/verdict.json", "verifier/trace.json", "verifier/world.sqlite", "verifier/reward.txt",
        "artifacts/arc-world-snapshot/snapshot.json", "artifacts/arc-world-snapshot/world.sqlite",
        "artifacts/arc-agent-isolation.json", "artifacts/manifest.json",
    }
    for trial in receipt["trials"]:
        task_id = trial["task"].split("/")[1]
        for relative in sorted(allowed):
            raw = (Path(job) / safe_relative(trial["trial"]) / relative).read_bytes()
            require(sha(raw) == trial["artifact_sha256"][relative], "job evidence changed during export")
            write(output / task_id / relative, raw)


def prepare_hf(frozen, local_job, registry_job, harbor, registry_receipt, output):
    frozen, harbor, output = Path(frozen).resolve(strict=True), Path(harbor).resolve(strict=True), Path(output).resolve()
    manifest = verify_freeze(frozen)
    require(manifest["version"] == VERSION, "publication builder version differs from freeze")
    verify_bundle(harbor)
    local, registry = admit(frozen, local_job), admit(frozen, registry_job, registry=True)
    identity = read_json(Path(registry_receipt))
    release = read_json(harbor / "release.json")
    frozen_digest = sha((frozen / "manifest.json").read_bytes())
    require(release["frozen_manifest_sha256"] == frozen_digest == identity.get("frozen_manifest_sha256"), "wrong publication freeze")
    require(release["source"] == manifest["source"] and release["tasks"] == manifest["tasks"], "publication source/tasks differ")
    require(read_json(harbor / "local-oracle-receipt.json") == public_receipt(local), "publication local receipt differs")
    client_digest = digest_dataset(tomllib.loads((harbor / "dataset.toml").read_text()))
    dataset_digest = identity.get("digest", "")
    require(identity.get("dataset") == DATASET and re.fullmatch(r"sha256:[a-f0-9]{64}", dataset_digest), "registry identity differs")
    require(identity.get("client_manifest_digest") == client_digest, "registry client manifest differs")
    require(identity.get("client_digest_matches_registry") is (client_digest == dataset_digest), "inaccurate registry/client hash disclosure")
    require(identity.get("exact_identity") is True and identity.get("tasks") == manifest["tasks"], "registry task identity missing")
    require(identity.get("publication_manifest_sha256") == sha((harbor / "publication-manifest.json").read_bytes()), "wrong registry wrapper")
    require(registry["dataset_reference"]["ref"] == dataset_digest, "registry run used different dataset")
    require(not output.exists() and not output.is_relative_to(frozen) and not output.is_relative_to(harbor), "new external HF directory required")
    output.mkdir(parents=True)
    shutil.copytree(frozen, output / "frozen")
    shutil.copytree(harbor, output / "harbor-publication")
    for name in ["release.json", "source-lock.json", "qualification.json", "local-oracle-receipt.json"]:
        write(output / name, (harbor / name).read_bytes())
    write(output / "registry-oracle-receipt.json", json_bytes(public_receipt(registry)))
    write(output / "registry-identity.json", json_bytes(identity))
    copy_evidence(frozen, local_job, local, output / "evidence/local")
    copy_evidence(frozen, registry_job, registry, output / "evidence/registry")
    rows = []
    for ref in manifest["tasks"]:
        task_id = ref["name"].split("/")[1]
        task = read_json(frozen / "tasks" / task_id / "task.json")
        rows.append({
            "task_id": task_id, "title": task["title"], "instruction": task["instruction"],
            "domain": "CRM and sales operations", "synthetic": True, "model_evaluated": False,
            "harbor_task": ref["name"], "harbor_digest": ref["digest"],
            "task_path": f"frozen/tasks/{task_id}/task.json",
            "assets_path": f"frozen/tasks/{task_id}/assets",
            "oracle_trajectory_path": f"evidence/registry/{task_id}/verifier/trace.json",
            "oracle_strict_pass": True, "oracle_score": 100,
        })
    write(output / "data/tasks.jsonl", b"".join(json_bytes(row) for row in rows))
    front = """---
license: apache-2.0
language: [en]
tags: [benchmark, synthetic, crm, mcp, tool-use]
pretty_name: Blobfish Arc CRM 6
size_categories: [n<1K]
configs:
- config_name: default
  data_files:
  - split: test
    path: data/tasks.jsonl
---

"""
    write(output / "README.md", front + card(manifest["source"], registry_digest=dataset_digest))
    seal(output)
    verify_freeze(frozen)
    verify_freeze(output / "frozen")
    return {"repo": HF_REPO, "files": len(inventory(output)), "registry_digest": dataset_digest}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["harbor", "hf", "verify"])
    parser.add_argument("output", type=Path)
    parser.add_argument("--frozen", type=Path)
    parser.add_argument("--local-job", type=Path)
    parser.add_argument("--registry-job", type=Path)
    parser.add_argument("--harbor", type=Path)
    parser.add_argument("--registry-receipt", type=Path)
    args = parser.parse_args()
    if args.command == "verify":
        result = {"files": len(verify_bundle(args.output)["files"]), "verified": True}
    elif args.command == "harbor":
        result = prepare_harbor(args.frozen, args.local_job, args.output)
    else:
        result = prepare_hf(args.frozen, args.local_job, args.registry_job, args.harbor, args.registry_receipt, args.output)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
