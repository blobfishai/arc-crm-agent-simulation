"""Freeze an independent local candidate. Publishing requires separate receipts."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from benchmark.hubbench.engine.assets import asset_bytes
from benchmark.hubbench.engine.families import public_tool_definitions
from benchmark.hubbench.engine.validation import canonical_json

from . import FAMILY, ROOT, SOURCE_LOCK, build_tasks
from .qualification import digest, qualify
from .runtime import implementation_hash
from .tools import SOURCE_NAMES


def public_task(task: dict) -> dict:
    keys = (
        "task_id",
        "family",
        "version",
        "workflow",
        "title",
        "role",
        "as_of",
        "world",
        "instruction",
        "starting_records",
        "answer_schema",
        "source_lineage",
    )
    return {key: task[key] for key in keys} | {
        "assets": [
            {
                key: value
                for key, value in record.items()
                if key not in {"content", "rows"}
            }
            for record in task["assets"]
        ]
    }


def verify_source(snapshot: Path) -> dict:
    """Hash exactly the pinned local card/data; never execute downloaded code."""
    checked = []
    for file in SOURCE_LOCK["source"]["files"]:
        path = snapshot / file["path"]
        raw = path.read_bytes()
        actual = hashlib.sha256(raw).hexdigest()
        if actual != file["sha256"] or ("bytes" in file and len(raw) != file["bytes"]):
            raise ValueError(f"source hash/size mismatch: {file['path']}")
        checked.append({"path": file["path"], "sha256": actual, "bytes": len(raw)})
    return {
        "source_id": SOURCE_LOCK["source"]["id"],
        "revision": SOURCE_LOCK["source"]["revision"],
        "verified_files": checked,
        "limitation": "Confirms local file identity, not current access/license terms or upstream code permission.",
    }


def freeze(output: Path) -> dict:
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise ValueError(
            "release destination must be new; frozen candidates are never overwritten"
        )
    tasks = build_tasks()
    report = qualify(tasks)
    if (
        not report["qualified"]
        or len(tasks) != SOURCE_LOCK["coverage"]["synthetic_tasks"]
    ):
        raise ValueError("local qualification or declared task membership failed")
    output.mkdir(parents=True, exist_ok=False)
    files = []

    def write(relative: str, raw: bytes) -> None:
        path = output / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as handle:
            handle.write(raw)
        files.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "bytes": len(raw),
            }
        )

    def write_json(relative: str, value: object) -> None:
        write(relative, (canonical_json(value) + "\n").encode())

    write_json("source-lock.json", SOURCE_LOCK)
    write_json("qualification.json", report)
    write_json(
        "tool-mapping.json",
        [
            {
                "name": tool.name,
                "source_observed_names": SOURCE_NAMES.get(tool.name, []),
                "contract_origin": "independently authored adaptation"
                if tool.name in SOURCE_NAMES
                else "Blobfish extension",
                "input_schema": tool.input_schema,
                "upstream_api_parity": False,
            }
            for tool in FAMILY.tools
        ],
    )
    write("README.md", (ROOT / "README.md").read_bytes())
    write("ENGINE-NOTICE", (ROOT.parents[2] / "hubbench" / "NOTICE").read_bytes())
    for task, result in zip(tasks, report["tasks"], strict=True):
        prefix = f"tasks/{task['task_id']}"
        write_json(f"{prefix}/task.json", public_task(task))
        write_json(
            f"{prefix}/tools.json",
            public_tool_definitions(FAMILY, task["answer_schema"]),
        )
        write_json(f"{prefix}/oracle-trajectory.json", result["oracle"]["trace"])
        write_json(f"{prefix}/oracle-verdict.json", result["oracle"]["verdict"])
        # Sealed material is a verifier/authoring artifact. This directory must
        # never be copied into an agent container or served as an agent resource.
        write_json(f"sealed/{task['task_id']}.json", task)
        for record in task["assets"]:
            write(f"{prefix}/{record['path']}", asset_bytes(record))
    manifest = {
        "schema_version": 1,
        "adapter": SOURCE_LOCK["adapter"],
        "version": FAMILY.version,
        "source_lock_sha256": digest(SOURCE_LOCK),
        "implementation_sha256": implementation_hash(),
        "status": "local-candidate",
        "publication_ready": False,
        "qualification_scope": report["scope"],
        "engine_metric": "HubScore",
        "task_ids": [task["task_id"] for task in tasks],
        "task_count": len(tasks),
        "tool_count": len(FAMILY.tools),
        "source_rows_reproduced": 0,
        "assets": sum(len(task["assets"]) for task in tasks),
        "remaining_gates": [
            "all four surfaces integration tests",
            "local Docker isolation and all-task oracle runs",
            "immutable source/Hugging Face/Harbor publication and registry receipts",
            "website disclosure of exact partial coverage",
        ],
        "files": sorted(files, key=lambda item: item["path"]),
    }
    with (output / "manifest.json").open("x", encoding="utf-8") as handle:
        handle.write(canonical_json(manifest) + "\n")
    return manifest


def verify_candidate(output: Path) -> dict:
    manifest = json.loads((output / "manifest.json").read_text())
    expected = {file["path"] for file in manifest["files"]} | {"manifest.json"}
    actual = {
        path.relative_to(output).as_posix()
        for path in output.rglob("*")
        if path.is_file()
    }
    if actual != expected:
        raise ValueError("candidate file membership differs from the manifest")
    for file in manifest["files"]:
        relative = Path(file["path"])
        path = output / relative
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or path.is_symlink()
            or not path.resolve().is_relative_to(output.resolve())
        ):
            raise ValueError("unsafe candidate member")
        raw = path.read_bytes()
        if (
            len(raw) != file["bytes"]
            or hashlib.sha256(raw).hexdigest() != file["sha256"]
        ):
            raise ValueError(f"candidate hash mismatch: {file['path']}")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=["freeze", "verify", "verify-source", "qualify"]
    )
    parser.add_argument("--path", type=Path)
    args = parser.parse_args()
    if args.command == "qualify":
        report = qualify()
        print(
            json.dumps(
                {key: value for key, value in report.items() if key != "tasks"},
                sort_keys=True,
            )
        )
        raise SystemExit(0 if report["qualified"] else 1)
    if args.path is None:
        parser.error("--path is required")
    result = {
        "freeze": freeze,
        "verify": verify_candidate,
        "verify-source": verify_source,
    }[args.command](args.path)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
