"""New-repository HF publication with exact pinned Git/LFS object admission."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from arc_release.build import json_bytes, sha
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


def run(root, *, publish=False, revision=None):
    from huggingface_hub import HfApi

    root = Path(root).resolve(strict=True)
    verify_bundle(root)
    api = HfApi()
    if publish:
        require(revision is None, "publish cannot use an arbitrary revision")
        if not api.repo_exists(HF_REPO, repo_type="dataset"):
            api.create_repo(HF_REPO, repo_type="dataset", private=False, exist_ok=False)
        info = api.dataset_info(HF_REPO, files_metadata=True)
        require(info.id == HF_REPO and info.private is False and not info.disabled and not info.gated, "HF repository unavailable or not public")
        paths = {sibling.rfilename for sibling in info.siblings}
        if paths - {".gitattributes"}:
            # Resume only if ALL existing bytes already match; never overwrite an
            # unrelated or partially populated repository automatically.
            receipt = verify_objects(root, info.siblings, info.sha)
        else:
            commit = api.upload_folder(repo_id=HF_REPO, repo_type="dataset", folder_path=root,
                                       commit_message=f"Publish qualified six-task Arc CRM {TAG}", parent_commit=info.sha)
            pinned = api.dataset_info(HF_REPO, revision=commit.oid, files_metadata=True)
            require(pinned.sha == commit.oid and pinned.id == HF_REPO, "HF returned different commit/repository")
            receipt = verify_objects(root, pinned.siblings, pinned.sha)
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
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    require(not args.output.exists() and not args.output.resolve().is_relative_to(args.publication.resolve()), "new external receipt path required")
    receipt = run(args.publication, publish=args.command == "publish", revision=args.revision)
    write(args.output, json_bytes(receipt))
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()
