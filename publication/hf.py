"""Exact HF publication; upgrades require a verified prior commit and parent CAS."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from arc_release.build import DATASET, VERSION, json_bytes, sha
from arc_release.receipts import require

from .bundle import HF_REPO, TAG, inventory, verify_bundle, write


def field(obj, key, default=None):
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def verify_objects(root, siblings, commit):
    require(re.fullmatch(r"[a-f0-9]{40}", commit), "exact HF commit required")
    verify_bundle(root)
    expected = {entry["path"]: entry for entry in inventory(root)}
    remote, metadata = {}, []
    for sibling in siblings:
        path = field(sibling, "rfilename")
        require(isinstance(path, str) and path and path not in remote and path not in metadata, "duplicate or invalid remote member")
        if path == ".gitattributes" and path not in expected:
            metadata.append(path)
        else:
            remote[path] = sibling
    require(set(remote) == set(expected), "HF payload membership differs")
    counts = {"git_blobs": 0, "lfs_objects": 0}
    for relative, entry in expected.items():
        obj = remote[relative]
        require(field(obj, "size") == entry["bytes"], f"HF size differs: {relative}")
        lfs = field(obj, "lfs")
        if lfs is not None:
            require(field(lfs, "sha256") == entry["sha256"], f"HF LFS hash differs: {relative}")
            counts["lfs_objects"] += 1
        else:
            raw = (root / relative).read_bytes()
            blob = hashlib.sha1(f"blob {len(raw)}\0".encode() + raw, usedforsecurity=False).hexdigest()
            require(field(obj, "blob_id") == blob, f"HF Git blob differs: {relative}")
            counts["git_blobs"] += 1
    verify_bundle(root)
    return {"repo": HF_REPO, "commit": commit, "exact_object_identity": True, "files_verified": len(expected),
            "payload_sha256": sha(json_bytes(list(expected.values()))), "platform_metadata_excluded": metadata,
            "model_evaluated": False, **counts}


def previous_inputs(root, publication, receipt):
    require((publication is None) == (receipt is None), "both previous HF inputs required")
    if publication is None:
        return None
    previous = Path(publication).resolve(strict=True)
    require(previous != root, "previous HF publication must be separate")
    verify_bundle(previous)
    identity = json.loads(Path(receipt).read_text())
    old = json.loads((previous / "release.json").read_text())
    current = json.loads((root / "release.json").read_text())
    require(old.get("dataset") == current.get("dataset") == DATASET, "previous HF dataset differs")
    version = old.get("version", "")
    require(re.fullmatch(r"0\.1\.[0-9]+", version) and current.get("version") == VERSION
            and int(VERSION.rsplit(".", 1)[1]) == int(version.rsplit(".", 1)[1]) + 1, "HF next patch required")
    require(identity.get("repo") == HF_REPO and re.fullmatch(r"[a-f0-9]{40}", identity.get("commit", "")), "previous HF identity differs")
    return {"root": previous, "identity": identity, "version": version}


def visible(info):
    require(info.id == HF_REPO and info.private is False and not info.disabled and not info.gated,
            "HF repository unavailable or not public")


def verify_previous(api, previous):
    identity = previous["identity"]
    info = api.dataset_info(HF_REPO, revision=identity["commit"], files_metadata=True)
    visible(info)
    require(info.sha == identity["commit"], "previous HF commit differs")
    verified = verify_objects(previous["root"], info.siblings, info.sha)
    require(all(identity.get(key) == value for key, value in verified.items()), "previous HF receipt differs")
    return verified


def run(root, *, publish=False, revision=None, previous_publication=None, previous_receipt=None, api=None):
    if api is None:
        from huggingface_hub import HfApi

        api = HfApi()

    root = Path(root).resolve(strict=True)
    verify_bundle(root)
    previous = previous_inputs(root, previous_publication, previous_receipt)
    require(previous is None or publish, "previous HF inputs are only for publication")
    if publish:
        require(revision is None, "publish cannot use an arbitrary revision")
        if not api.repo_exists(HF_REPO, repo_type="dataset"):
            require(previous is None, "previous HF repository disappeared")
            api.create_repo(HF_REPO, repo_type="dataset", private=False, exist_ok=False)
        info = api.dataset_info(HF_REPO, files_metadata=True)
        visible(info)
        paths = {sibling.rfilename for sibling in info.siblings}
        upgrade = False
        if previous:
            verify_previous(api, previous)
            if info.sha == previous["identity"]["commit"]:
                verify_objects(previous["root"], info.siblings, info.sha)
                upgrade = True
        if paths - {".gitattributes"} and not upgrade:
            # Resume only if ALL existing bytes already match; never overwrite an
            # unrelated or partially populated repository automatically.
            receipt = verify_objects(root, info.siblings, info.sha)
        else:
            from huggingface_hub import CommitOperationAdd, CommitOperationDelete

            require(previous is None or upgrade, "previous HF HEAD moved or emptied")
            removed = sorted({entry["path"] for entry in inventory(previous["root"])} -
                             {entry["path"] for entry in inventory(root)}) if previous else []
            require(not any(any(char in path for char in "*?[]") for path in removed), "ambiguous HF removal pattern")
            operations = [CommitOperationAdd(path_in_repo=entry["path"], path_or_fileobj=root / entry["path"])
                          for entry in inventory(root)]
            operations.extend(CommitOperationDelete(path_in_repo=path, is_folder=False) for path in removed)
            # upload_folder may split one upload into multiple commits. Its parent
            # guard applies only to the first. Our small release uses one explicit
            # commit so compare-and-swap protects the entire replacement.
            commit = api.create_commit(repo_id=HF_REPO, repo_type="dataset", revision="main", create_pr=False,
                                       operations=operations, commit_message=f"Publish qualified six-task Arc CRM {TAG}",
                                       parent_commit=info.sha)
            pinned = api.dataset_info(HF_REPO, revision=commit.oid, files_metadata=True)
            require(pinned.sha == commit.oid and pinned.id == HF_REPO, "HF returned different commit/repository")
            visible(pinned)
            receipt = verify_objects(root, pinned.siblings, pinned.sha)
        if previous:
            prior = verify_previous(api, previous)
            receipt["previous_release"] = {"version": previous["version"], "commit": prior["commit"],
                                           "payload_sha256": prior["payload_sha256"], "immutable_objects_preserved": True}
    else:
        require(revision and re.fullmatch(r"[a-f0-9]{40}", revision), "verification requires pinned commit")
        pinned = api.dataset_info(HF_REPO, revision=revision, files_metadata=True)
        require(pinned.sha == revision and pinned.id == HF_REPO and pinned.private is False and not pinned.disabled and not pinned.gated,
                "HF commit/repository visibility differs")
        receipt = verify_objects(root, pinned.siblings, pinned.sha)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["publish", "verify"])
    parser.add_argument("publication", type=Path)
    parser.add_argument("--revision")
    parser.add_argument("--previous-publication", type=Path)
    parser.add_argument("--previous-receipt", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    require(not args.output.exists() and not args.output.resolve().is_relative_to(args.publication.resolve())
            and (args.previous_publication is None or not args.output.resolve().is_relative_to(args.previous_publication.resolve())),
            "new external receipt path required")
    receipt = run(args.publication, publish=args.command == "publish", revision=args.revision,
                  previous_publication=args.previous_publication, previous_receipt=args.previous_receipt)
    write(args.output, json_bytes(receipt))
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()
