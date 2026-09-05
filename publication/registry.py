"""Publish only the admitted six-task release; verify tags, metadata and objects."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import tomllib
from pathlib import Path

from arc_release.build import DATASET, content_hash, json_bytes, sha, verify_freeze
from arc_release.receipts import admit, read_json, require

from .bundle import TAG, digest_dataset, public_receipt, verify_bundle, write


def check_package(package, kind, digest, version, *, tag=TAG, require_latest=True):
    require(package.get("type") == kind and package.get("visibility") == "public", "wrong package type or visibility")
    require(package.get("org", {}).get("name") == "blobfishai", "wrong package organization")
    require(version.get("content_hash") == digest and version.get("yanked_at") is None, "wrong or yanked version")
    require(tag in version.get("tags", []) and (not require_latest or "latest" in version["tags"]), "requested public tags did not resolve")


def registry_identifier(version):
    """Registry identifier, NOT an independently reproduced content hash.

    Harbor 0.21.0 sends members to publish_dataset_version and accepts its
    returned identifier. The deployed formula is not public and differs from
    DatasetManifest.compute_content_hash for this release. Member SHA256s and
    exact metadata must still be independently verified before admission.
    """
    digest = version.get("content_hash", "")
    require(re.fullmatch(r"sha256:[a-f0-9]{64}", digest), "invalid registry dataset identifier")
    return digest


def check_files(rows, expected, *, dataset=False):
    actual = []
    for row in rows:
        actual.append({"path": row["path"], "sha256": row["content_hash"].removeprefix("sha256:"), "bytes": row["size_bytes"]})
        if dataset:
            require(row.get("storage_path") == f"packages/{DATASET}/{actual[-1]['sha256']}/{row['path']}", "wrong dataset storage identity")
    require(sorted(actual, key=lambda x: x["path"]) == sorted(expected, key=lambda x: x["path"]), "remote file membership/size/digest mismatch")


def validate_task(package, version, task, reference, *, tag=TAG, require_latest=True):
    from harbor.models.task.config import TaskConfig

    check_package(package, "task", reference["digest"], version, tag=tag, require_latest=require_latest)
    require(package.get("name") == reference["name"].split("/")[1], "wrong task package name")
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


def validate_dataset(package, version, root, *, tag=TAG, require_latest=True):
    from harbor.models.dataset.manifest import DatasetManifest

    manifest = DatasetManifest.from_toml_file(root / "dataset.toml")
    client_digest = "sha256:" + manifest.compute_content_hash()
    require(client_digest == digest_dataset(tomllib.loads((root / "dataset.toml").read_text())), "dataset hashing disagrees with Harbor client")
    digest = registry_identifier(version)
    check_package(package, "dataset", digest, version, tag=tag, require_latest=require_latest)
    require(package.get("name") == DATASET.split("/")[1], "wrong dataset package name")
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


def publication_binding(frozen, root, manifest):
    verify_bundle(root)
    release = read_json(root / "release.json")
    require(release["frozen_manifest_sha256"] == sha((frozen / "manifest.json").read_bytes()), "publication binds different freeze")
    require(release["source"] == manifest["source"] and release["tasks"] == manifest["tasks"], "publication source/tasks differ")
    require(release["dataset"] == DATASET and release["version"] == manifest["version"], "publication version differs")
    descriptor = tomllib.loads((root / "dataset.toml").read_text())
    require(descriptor["dataset"]["name"] == DATASET and descriptor["dataset"]["version"] == manifest["version"], "unexpected publication namespace/version")
    require(descriptor["tasks"] == [{"name": ref["name"], "digest": ref["digest"]} for ref in manifest["tasks"]], "unexpected publication task references")
    return descriptor


def previous_release(frozen, root, receipt, current_version):
    require(all(value is not None for value in [frozen, root, receipt]), "all three previous-release inputs required")
    frozen, root = Path(frozen).resolve(strict=True), Path(root).resolve(strict=True)
    manifest = verify_freeze(frozen)
    descriptor = publication_binding(frozen, root, manifest)
    identity = read_json(Path(receipt))
    old_version = manifest["version"]
    require(tuple(map(int, current_version.split("."))) == (0, 1, int(old_version.split(".")[2]) + 1), "only an explicit next patch upgrade is supported")
    require(not manifest["source"]["dirty"], "previous release must bind clean source")
    require(identity.get("dataset") == DATASET and identity.get("tag") == "v" + old_version
            and identity.get("exact_identity") is True, "previous registry identity missing")
    require(identity.get("tasks") == manifest["tasks"] and identity.get("frozen_manifest_sha256") == sha((frozen / "manifest.json").read_bytes()), "previous registry freeze differs")
    require(identity.get("publication_manifest_sha256") == sha((root / "publication-manifest.json").read_bytes()), "previous registry wrapper differs")
    client = digest_dataset(descriptor)
    require(identity.get("client_manifest_digest") == client and identity.get("client_digest_matches_registry") is (client == identity.get("digest")), "previous client manifest differs")
    require(re.fullmatch(r"sha256:[a-f0-9]{64}", identity.get("digest", "")), "invalid previous registry identifier")
    return {"frozen": frozen, "root": root, "manifest": manifest, "identity": identity, "tag": "v" + old_version}


def transition(tags, *, previous_tag=None):
    """Only create a new tag or resume exact current bytes; never move an old tag."""
    mapping = {}
    for row in tags:
        name, revision = row.get("tag"), row.get("revision")
        require(isinstance(name, str) and name not in mapping and type(revision) is int and revision > 0, "invalid or duplicate registry tags")
        mapping[name] = revision
    require("latest" in mapping, "existing package is missing latest")
    if previous_tag is not None:
        require(previous_tag != TAG and previous_tag in mapping, "previous version tag missing")
    if TAG in mapping:
        require(mapping["latest"] == mapping[TAG], "latest moved outside the current release")
        require(previous_tag is None or mapping[previous_tag] < mapping[TAG], "previous tag was moved or reused")
        return "current"
    require(previous_tag is not None and mapping["latest"] == mapping[previous_tag], "existing package needs exact explicit previous-release authority")
    return "upgrade"


async def verify_previous_remote(db, name, kind, previous, *, require_latest=False):
    p, v = await resolve(db, name, previous["tag"], dataset=kind == "dataset")
    if kind == "dataset":
        digest = validate_dataset(p, v, previous["root"], tag=previous["tag"], require_latest=require_latest)
        require(digest == previous["identity"]["digest"], "previous dataset tag moved")
    else:
        reference = next(ref for ref in previous["manifest"]["tasks"] if ref["name"] == name)
        task = previous["frozen"] / "harbor/tasks" / name.split("/")[1]
        validate_task(p, v, task, reference, tag=previous["tag"], require_latest=require_latest)
        digest = reference["digest"]
    immutable = await resolve(db, name, digest, dataset=kind == "dataset")
    require(immutable == (p, v), "previous immutable resolution differs from tag")


async def inspect_target(db, name, kind, frozen, root, references, previous):
    package = await db.get_package(org="blobfishai", name=name.split("/")[1])
    if package is None:
        require(previous is None, "previous package disappeared")
        return "new"
    require(package.get("type") == kind, "wrong existing package type")
    tags = await db.list_package_tags(org="blobfishai", name=name.split("/")[1], package_type=kind)
    state = transition(tags, previous_tag=previous["tag"] if previous else None)
    if previous:
        await verify_previous_remote(db, name, kind, previous, require_latest=state == "upgrade")
    if state == "current":
        p, v = await resolve(db, name, dataset=kind == "dataset")
        if kind == "dataset":
            validate_dataset(p, v, root)
        else:
            reference = next(ref for ref in references if ref["name"] == name)
            validate_task(p, v, frozen / "harbor/tasks" / name.split("/")[1], reference)
    return state


async def run(frozen, local_job, root, *, publish=False, previous_frozen=None, previous_publication=None, previous_receipt=None):
    from harbor.db.client import RegistryDB
    from harbor.publisher.publisher import Publisher

    frozen, root = Path(frozen).resolve(strict=True), Path(root).resolve(strict=True)
    manifest = verify_freeze(frozen)
    local = admit(frozen, local_job)
    require("v" + manifest["version"] == TAG, "publication tool version differs from freeze")
    descriptor = publication_binding(frozen, root, manifest)
    require(read_json(root / "local-oracle-receipt.json") == public_receipt(local), "publication local receipt differs")
    client_digest = digest_dataset(descriptor)
    previous = None
    if any(value is not None for value in [previous_frozen, previous_publication, previous_receipt]):
        previous = previous_release(previous_frozen, previous_publication, previous_receipt, manifest["version"])
    db = RegistryDB()
    targets = [(ref["name"], "task") for ref in manifest["tasks"]] + [(DATASET, "dataset")]
    # Preflight every target before the first mutation. Auth errors are not absence.
    for name, kind in targets:
        state = await inspect_target(db, name, kind, frozen, root, manifest["tasks"], previous)
        require(publish or state == "current", f"unpublished release: {name}")
    publisher = Publisher() if publish else None
    verified = []
    for ref in manifest["tasks"]:
        task = frozen / "harbor/tasks" / ref["name"].split("/")[1]
        # Recheck immediately before each mutation; never rely only on the initial
        # seven-name preflight. The registry has no cross-package atomic CAS.
        state = await inspect_target(db, ref["name"], "task", frozen, root, manifest["tasks"], previous)
        if publish and state != "current":
            result = await publisher.publish_task(task, tags={TAG}, visibility="public")
            require("sha256:" + result.content_hash.removeprefix("sha256:") == ref["digest"], "publisher task digest differs")
        package, version = await resolve(db, ref["name"])
        verified.append(validate_task(package, version, task, ref))
        # Resolve immutably as well as through the mutable tag.
        p, v = await resolve(db, ref["name"], ref["digest"])
        validate_task(p, v, task, ref)
        print(json.dumps({"verified_task": ref["name"], "digest": ref["digest"]}), flush=True)
    published_identifier = None
    state = await inspect_target(db, DATASET, "dataset", frozen, root, manifest["tasks"], previous)
    if publish and state != "current":
        result = await publisher.publish_dataset(root, tags={TAG}, visibility="public", promote_tasks=True)
        published_identifier = "sha256:" + result.content_hash.removeprefix("sha256:")
    package, version = await resolve(db, DATASET, dataset=True)
    digest = validate_dataset(package, version, root)
    require(published_identifier is None or published_identifier == digest, "publisher returned different registry identity")
    p, v = await resolve(db, DATASET, digest, dataset=True)
    require(validate_dataset(p, v, root) == digest and p == package and v == version, "immutable registry resolution differs from tag")
    if previous:
        for name, kind in targets:
            await verify_previous_remote(db, name, kind, previous)
        verify_freeze(previous["frozen"])
        verify_bundle(previous["root"])
    verify_bundle(root)
    verify_freeze(frozen)
    return {
        "schema_version": 1, "dataset": DATASET, "digest": digest, "tag": TAG,
        "revision": version["revision"], "visibility": package["visibility"], "exact_identity": True,
        "client_manifest_digest": client_digest, "client_digest_matches_registry": client_digest == digest,
        "registry_digest_semantics": "Opaque registry-assigned version identifier; not a locally reproduced content commitment.",
        "identity_basis": "Every task config/name/digest and dataset file path/size/SHA256/storage path independently verified; tag and immutable resolutions identical.",
        "frozen_manifest_sha256": sha((frozen / "manifest.json").read_bytes()),
        "publication_manifest_sha256": sha((root / "publication-manifest.json").read_bytes()),
        "tasks": manifest["tasks"], "task_objects": verified,
        "dataset_files_verified": len(version["files"]), "model_evaluated": False,
        "registry_execution_qualified": False,
        "previous_release": {"tag": previous["tag"], "digest": previous["identity"]["digest"], "all_seven_tags_preserved": True} if previous else None,
        "scope": "Registry identities and object metadata only; all-six registry execution needs a separate receipt.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["publish", "verify"])
    parser.add_argument("frozen", type=Path)
    parser.add_argument("local_job", type=Path)
    parser.add_argument("publication", type=Path)
    parser.add_argument("--previous-frozen", type=Path)
    parser.add_argument("--previous-publication", type=Path)
    parser.add_argument("--previous-receipt", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    input_roots = [args.frozen, args.publication, args.previous_frozen, args.previous_publication]
    require(not args.output.exists() and not args.output.is_symlink()
            and not any(args.output.resolve().is_relative_to(root.resolve()) for root in input_roots if root is not None),
            "new external receipt path required")
    result = asyncio.run(run(args.frozen, args.local_job, args.publication, publish=args.command == "publish",
                            previous_frozen=args.previous_frozen, previous_publication=args.previous_publication,
                            previous_receipt=args.previous_receipt))
    write(args.output, json_bytes(result))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
