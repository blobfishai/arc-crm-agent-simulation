"""Publish only the admitted six-task release; verify tags, metadata and objects."""

from __future__ import annotations

import argparse
import asyncio
import json
import tomllib
from pathlib import Path

from arc_release.build import DATASET, content_hash, json_bytes, sha, verify_freeze
from arc_release.receipts import admit, read_json, require

from .bundle import TAG, digest_dataset, public_receipt, verify_bundle, write


def check_package(package, kind, digest, version):
    require(package.get("type") == kind and package.get("visibility") == "public", "wrong package type or visibility")
    require(package.get("org", {}).get("name") == "blobfishai", "wrong package organization")
    require(version.get("content_hash") == digest and version.get("yanked_at") is None, "wrong or yanked version")
    require(TAG in version.get("tags", []) and "latest" in version["tags"], "requested public tags did not resolve")


def check_files(rows, expected, *, dataset=False):
    actual = []
    for row in rows:
        actual.append({"path": row["path"], "sha256": row["content_hash"].removeprefix("sha256:"), "bytes": row["size_bytes"]})
        if dataset:
            require(row.get("storage_path") == f"packages/{DATASET}/{actual[-1]['sha256']}/{row['path']}", "wrong dataset storage identity")
    require(sorted(actual, key=lambda x: x["path"]) == sorted(expected, key=lambda x: x["path"]), "remote file membership/size/digest mismatch")


def validate_task(package, version, task, reference):
    from harbor.models.task.config import TaskConfig

    check_package(package, "task", reference["digest"], version)
    config = TaskConfig.model_validate_toml((task / "task.toml").read_text()).model_dump(mode="json")
    require(version.get("config") == config, "remote task config differs from qualified package")
    for key in ["description", "authors", "keywords"]:
        require(version.get(key) == config["task"][key], f"remote task {key} mismatch")
    require(version.get("instruction") == (task / "instruction.md").read_text(), "wrong task instruction")
    require(version.get("readme") == (task / "README.md").read_text(), "wrong task README")
    digest, files = content_hash(task)
    require(digest == reference["digest"], "task changed during remote verification")
    expected = [{"path": relative, "sha256": sha((task / relative).read_bytes()), "bytes": (task / relative).stat().st_size} for relative in files]
    check_files(version.get("files", []), expected)
    return {"name": reference["name"], "digest": digest, "revision": version["revision"], "files_verified": len(files)}


def validate_dataset(package, version, root):
    from harbor.models.dataset.manifest import DatasetManifest

    manifest = DatasetManifest.from_toml_file(root / "dataset.toml")
    digest = "sha256:" + manifest.compute_content_hash()
    require(digest == digest_dataset(tomllib.loads((root / "dataset.toml").read_text())), "dataset hashing disagrees with Harbor")
    check_package(package, "dataset", digest, version)
    require(version.get("description") == manifest.dataset.description, "wrong dataset description")
    require(version.get("authors") == [author.model_dump(mode="json") for author in manifest.dataset.authors], "wrong dataset authors")
    require(version.get("readme") == (root / "README.md").read_text(), "wrong dataset README")
    actual = []
    for row in version.get("tasks", []):
        task = row["task_version"]
        actual.append({"name": f"{task['package']['org']['name']}/{task['package']['name']}", "digest": "sha256:" + task["content_hash"].removeprefix("sha256:")})
    require(sorted(actual, key=lambda x: x["name"]) == sorted([ref.model_dump() for ref in manifest.tasks], key=lambda x: x["name"]), "wrong dataset task membership")
    expected = [{"path": ref.path, "sha256": sha((root / ref.path).read_bytes()), "bytes": (root / ref.path).stat().st_size} for ref in manifest.files]
    check_files(version.get("files", []), expected, dataset=True)
    return digest


async def resolve(db, name, ref=TAG, *, dataset=False):
    org, short = name.split("/")
    return await db.get_package_version(org=org, name=short, ref=ref, include_files=True, include_tasks=dataset)


async def run(frozen, local_job, root, *, publish=False):
    from harbor.db.client import RegistryDB
    from harbor.publisher.publisher import Publisher

    frozen, root = Path(frozen).resolve(strict=True), Path(root).resolve(strict=True)
    manifest = verify_freeze(frozen)
    verify_bundle(root)
    local = admit(frozen, local_job)
    release = read_json(root / "release.json")
    require(release["frozen_manifest_sha256"] == sha((frozen / "manifest.json").read_bytes()), "publication binds different freeze")
    require(release["source"] == manifest["source"] and release["tasks"] == manifest["tasks"], "publication source/tasks differ")
    require(read_json(root / "local-oracle-receipt.json") == public_receipt(local), "publication local receipt differs")
    descriptor = tomllib.loads((root / "dataset.toml").read_text())
    require(descriptor["dataset"]["name"] == DATASET, "unexpected publication namespace")
    require(descriptor["tasks"] == [{"name": ref["name"], "digest": ref["digest"]} for ref in manifest["tasks"]], "unexpected publication task references")
    digest = digest_dataset(descriptor)
    db, existing = RegistryDB(), {}
    # Preflight every target before the first mutation. Auth errors are not absence.
    for name, kind in [(ref["name"], "task") for ref in manifest["tasks"]] + [(DATASET, "dataset")]:
        package = await db.get_package(org="blobfishai", name=name.split("/")[1])
        existing[name] = package is not None
        if package is not None:
            p, v = await resolve(db, name, dataset=kind == "dataset")
            if kind == "dataset":
                validate_dataset(p, v, root)
            else:
                ref = next(ref for ref in manifest["tasks"] if ref["name"] == name)
                validate_task(p, v, frozen / "harbor/tasks" / name.split("/")[1], ref)
        elif not publish:
            raise ValueError(f"unpublished package: {name}")
    publisher = Publisher() if publish else None
    verified = []
    for ref in manifest["tasks"]:
        task = frozen / "harbor/tasks" / ref["name"].split("/")[1]
        if publish and not existing[ref["name"]]:
            result = await publisher.publish_task(task, tags={TAG}, visibility="public")
            require("sha256:" + result.content_hash.removeprefix("sha256:") == ref["digest"], "publisher task digest differs")
        package, version = await resolve(db, ref["name"])
        verified.append(validate_task(package, version, task, ref))
        # Resolve immutably as well as through the mutable tag.
        p, v = await resolve(db, ref["name"], ref["digest"])
        validate_task(p, v, task, ref)
        print(json.dumps({"verified_task": ref["name"], "digest": ref["digest"]}), flush=True)
    if publish and not existing[DATASET]:
        result = await publisher.publish_dataset(root, tags={TAG}, visibility="public", promote_tasks=True)
        require("sha256:" + result.content_hash.removeprefix("sha256:") == digest, "publisher dataset digest differs")
    package, version = await resolve(db, DATASET, dataset=True)
    validate_dataset(package, version, root)
    p, v = await resolve(db, DATASET, digest, dataset=True)
    validate_dataset(p, v, root)
    verify_bundle(root)
    verify_freeze(frozen)
    return {
        "schema_version": 1, "dataset": DATASET, "digest": digest, "tag": TAG,
        "revision": version["revision"], "visibility": package["visibility"], "exact_identity": True,
        "frozen_manifest_sha256": sha((frozen / "manifest.json").read_bytes()),
        "publication_manifest_sha256": sha((root / "publication-manifest.json").read_bytes()),
        "tasks": manifest["tasks"], "task_objects": verified,
        "dataset_files_verified": len(version["files"]), "model_evaluated": False,
        "registry_execution_qualified": False,
        "scope": "Registry identities and object metadata only; all-six registry execution needs a separate receipt.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["publish", "verify"])
    parser.add_argument("frozen", type=Path)
    parser.add_argument("local_job", type=Path)
    parser.add_argument("publication", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    require(not args.output.exists() and not args.output.resolve().is_relative_to(args.frozen.resolve())
            and not args.output.resolve().is_relative_to(args.publication.resolve()), "new external receipt path required")
    result = asyncio.run(run(args.frozen, args.local_job, args.publication, publish=args.command == "publish"))
    write(args.output, json_bytes(result))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
