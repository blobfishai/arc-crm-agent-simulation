import copy
import hashlib
import json
import shutil
import tomllib
from pathlib import Path

import pytest
from test_receipts import saved_fixture  # noqa: F401 — shared synthetic job fixture

from arc_release.build import DATASET, json_bytes, sha, verify_freeze
from arc_release.receipts import admit
from publication import bundle
from publication.hf import verify_objects
from publication.registry import check_files, check_package, registry_identifier


@pytest.fixture
def wrapper(request, tmp_path, monkeypatch):
    frozen, job = request.getfixturevalue("saved_fixture")
    # Unit fixtures simulate Harbor; they never establish a release qualification.
    monkeypatch.setattr(bundle, "admit", lambda frozen, job, **kw: admit(frozen, job, allow_dirty=True, **kw))
    output = tmp_path / "publication"
    result = bundle.prepare_harbor(frozen, job, output)
    return frozen, job, output, result


def test_wrapper_preserves_all_frozen_bytes_and_pins_six_tasks(wrapper):
    frozen, job, output, result = wrapper
    manifest = verify_freeze(frozen)
    descriptor = tomllib.loads((output / "dataset.toml").read_text())
    assert descriptor["tasks"] == [{"name": ref["name"], "digest": ref["digest"]} for ref in manifest["tasks"]]
    assert result == {"dataset": DATASET, "digest": bundle.digest_dataset(descriptor), "tasks": 6}
    assert len(descriptor["files"]) == 5
    bundle.verify_bundle(output)
    receipt = json.loads((output / "local-oracle-receipt.json").read_text())
    assert "job" not in receipt and "path" not in receipt["dataset_reference"]
    assert receipt["model_evaluated"] is False
    assert receipt["original_receipt_sha256"] == sha(json_bytes(admit(frozen, job, allow_dirty=True)))
    card = (output / "README.md").read_text()
    for limitation in ["zero source rows", "not model leaderboard", "not upstream API/ABI parity", "not a Docker-enforced disk quota"]:
        assert limitation in card


def test_new_only_and_outside_freeze(wrapper):
    frozen, job, output, _ = wrapper
    with pytest.raises(ValueError, match="new external"):
        bundle.prepare_harbor(frozen, job, output)
    with pytest.raises(ValueError, match="new external"):
        bundle.prepare_harbor(frozen, job, frozen / "publication")
    verify_freeze(frozen)


def test_output_parent_symlink_cannot_bypass_frozen_boundary(wrapper, tmp_path):
    frozen, job, _, _ = wrapper
    alias = tmp_path / "alias"
    alias.symlink_to(frozen, target_is_directory=True)
    with pytest.raises(ValueError, match="new external"):
        bundle.prepare_harbor(frozen, job, alias / "publication")
    verify_freeze(frozen)


@pytest.mark.parametrize("mutation", ["changed", "extra", "deleted", "symlink"])
def test_publication_identity_is_fail_closed(wrapper, mutation):
    _, _, output, _ = wrapper
    if mutation == "changed":
        (output / "README.md").write_text("changed")
    elif mutation == "extra":
        (output / "extra.txt").write_text("unexpected")
    elif mutation == "deleted":
        (output / "README.md").unlink()
    else:
        (output / "link").symlink_to(output / "README.md")
    with pytest.raises(ValueError):
        bundle.verify_bundle(output)


def test_dataset_hash_includes_files_not_just_task_hashes():
    descriptor = {"tasks": [{"name": "a/b", "digest": "sha256:" + "1" * 64}], "files": []}
    assert bundle.digest_dataset(descriptor) == "sha256:" + sha(("1" * 64).encode())
    descriptor["files"] = [{"path": "README.md", "digest": "sha256:" + "2" * 64}]
    expected = "1" * 64 + ";README.md:" + "2" * 64
    assert bundle.digest_dataset(descriptor) == "sha256:" + sha(expected.encode())


@pytest.fixture
def hf_objects(tmp_path):
    root = tmp_path / "hf"
    root.mkdir()
    (root / "README.md").write_text("no model rankings")
    (root / "binary.sqlite").write_bytes(b"mock binary")
    bundle.seal(root)
    objects = []
    for entry in bundle.inventory(root):
        obj = {"rfilename": entry["path"], "size": entry["bytes"]}
        if entry["path"] == "binary.sqlite":
            obj["lfs"] = {"sha256": entry["sha256"]}
        else:
            raw = (root / entry["path"]).read_bytes()
            obj["blob_id"] = hashlib.sha1(f"blob {len(raw)}\0".encode() + raw, usedforsecurity=False).hexdigest()
        objects.append(obj)
    objects.append({"rfilename": ".gitattributes"})
    return root, objects


def test_all_git_and_lfs_objects_verified(hf_objects):
    root, objects = hf_objects
    receipt = verify_objects(root, objects, "a" * 40)
    assert receipt["files_verified"] == 3
    assert receipt["git_blobs"] == 2 and receipt["lfs_objects"] == 1
    assert receipt["platform_metadata_excluded"] == [".gitattributes"]
    assert receipt["exact_object_identity"] is True and receipt["model_evaluated"] is False


@pytest.mark.parametrize("mutation", ["git", "lfs", "size", "extra", "missing", "duplicate", "duplicate-metadata", "bad-path"])
def test_hf_mismatch_rejected(hf_objects, mutation):
    root, objects = hf_objects
    if mutation == "git":
        next(obj for obj in objects if "blob_id" in obj)["blob_id"] = "f" * 40
    elif mutation == "lfs":
        next(obj for obj in objects if "lfs" in obj)["lfs"]["sha256"] = "f" * 64
    elif mutation == "size":
        objects[0]["size"] += 1
    elif mutation == "extra":
        objects.append({"rfilename": "unexpected"})
    elif mutation == "missing":
        objects.pop(0)
    elif mutation == "duplicate":
        objects.append(objects[0])
    elif mutation == "duplicate-metadata":
        objects.append(objects[-1])
    else:
        objects[0]["rfilename"] = None
    with pytest.raises(ValueError):
        verify_objects(root, objects, "a" * 40)


@pytest.mark.parametrize("commit", ["main", "v0.1.0", "a" * 39, "Z" * 40, "a" * 41])
def test_hf_mutable_or_invalid_commit_rejected(hf_objects, commit):
    with pytest.raises(ValueError, match="exact HF commit"):
        verify_objects(*hf_objects, commit)


@pytest.mark.parametrize("key,value", [("type", "dataset"), ("visibility", "private"), ("org", {"name": "other"})])
def test_registry_wrong_namespace_type_visibility_rejected(key, value):
    package = {"type": "task", "visibility": "public", "org": {"name": "blobfishai"}, key: value}
    with pytest.raises(ValueError):
        check_package(package, "task", "sha256:" + "a" * 64, {"content_hash": "sha256:" + "a" * 64, "tags": ["latest", "v0.1.0"]})


@pytest.mark.parametrize("key,value", [("content_hash", "sha256:" + "b" * 64), ("yanked_at", "today"), ("tags", ["latest"]), ("tags", ["v0.1.0"])])
def test_registry_moved_missing_tag_or_yank_rejected(key, value):
    package = {"type": "task", "visibility": "public", "org": {"name": "blobfishai"}}
    version = {"content_hash": "sha256:" + "a" * 64, "tags": ["latest", "v0.1.0"], key: value}
    with pytest.raises(ValueError):
        check_package(package, "task", "sha256:" + "a" * 64, version)


@pytest.mark.parametrize("value", ["latest", "v0.1.0", "a" * 64, "sha256:" + "g" * 64, "sha256:" + "a" * 63])
def test_registry_identifier_requires_exact_immutable_ref(value):
    with pytest.raises(ValueError, match="invalid registry dataset identifier"):
        registry_identifier({"content_hash": value})


def test_registry_identifier_is_distinct_from_the_local_manifest_hash():
    assert registry_identifier({"content_hash": "sha256:" + "a" * 64}) == "sha256:" + "a" * 64


@pytest.mark.parametrize("mutation", ["digest", "size", "duplicate", "extra", "missing", "storage"])
def test_registry_object_identity_rejected(mutation):
    expected = [{"path": "release.json", "sha256": "a" * 64, "bytes": 3}]
    rows = [{"path": "release.json", "content_hash": "a" * 64, "size_bytes": 3, "storage_path": f"packages/{DATASET}/{'a' * 64}/release.json"}]
    check_files(rows, expected, dataset=True)
    rows = copy.deepcopy(rows)
    if mutation == "digest":
        rows[0]["content_hash"] = "b" * 64
    elif mutation == "size":
        rows[0]["size_bytes"] = 4
    elif mutation == "duplicate":
        rows.append(rows[0])
    elif mutation == "extra":
        rows.append({"path": "extra", "content_hash": "b" * 64, "size_bytes": 4})
    elif mutation == "missing":
        rows.clear()
    else:
        rows[0]["storage_path"] = "another/package"
    with pytest.raises(ValueError):
        check_files(rows, expected, dataset=True)


def test_evidence_export_omits_workstation_and_agent_workspace(wrapper, tmp_path):
    frozen, job, _, _ = wrapper
    receipt = admit(frozen, job, allow_dirty=True)
    output = tmp_path / "evidence"
    bundle.copy_evidence(frozen, job, receipt, output)
    files = bundle.inventory(output)
    assert len(files) == 66
    assert not any(Path(entry["path"]).name in {"config.json", "lock.json"} for entry in files)
    assert sum(entry["path"].endswith("verifier/trace.json") for entry in files) == 6


def test_complete_hf_wrapper_contains_flat_preview_and_both_admitted_jobs(wrapper, tmp_path):
    frozen, local, harbor, result = wrapper
    registry = tmp_path / "registry-job"
    shutil.copytree(local, registry)
    registry_digest = "sha256:" + "a" * 64
    descriptor = {"name": DATASET, "ref": registry_digest}
    config = json.loads((registry / "config.json").read_text())
    config["datasets"] = [descriptor]
    (registry / "config.json").write_bytes(json_bytes(config))
    locks = []
    for directory in sorted(registry.glob("arc-crm-*")):
        lock = json.loads((directory / "lock.json").read_text())
        task_id = lock["task"]["name"]
        digest = lock["task"]["digest"]
        lock["task"] = {"name": f"blobfishai/{task_id}", "type": "package", "source": DATASET, "version": "0.1.0", "digest": digest}
        (directory / "lock.json").write_bytes(json_bytes(lock))
        locks.append(lock)
        trial = json.loads((directory / "result.json").read_text())
        trial["source"] = DATASET
        trial["task_id"] = {"org": "blobfishai", "name": task_id, "ref": digest}
        (directory / "result.json").write_bytes(json_bytes(trial))
    job_lock = json.loads((registry / "lock.json").read_text())
    job_lock["trials"] = locks
    (registry / "lock.json").write_bytes(json_bytes(job_lock))
    manifest = verify_freeze(frozen)
    identity = {
        "dataset": DATASET, "digest": registry_digest, "client_manifest_digest": result["digest"],
        "client_digest_matches_registry": False, "tasks": manifest["tasks"], "exact_identity": True,
        "frozen_manifest_sha256": sha((frozen / "manifest.json").read_bytes()),
        "publication_manifest_sha256": sha((harbor / "publication-manifest.json").read_bytes()),
    }
    identity_path = tmp_path / "identity.json"
    identity_path.write_bytes(json_bytes(identity))
    output = tmp_path / "hf-release"
    result = bundle.prepare_hf(frozen, local, registry, harbor, identity_path, output)
    bundle.verify_bundle(output)
    verify_freeze(output / "frozen")
    rows = [json.loads(line) for line in (output / "data/tasks.jsonl").read_text().splitlines()]
    assert len(rows) == 6 and len({row["task_id"] for row in rows}) == 6
    assert all(not row["model_evaluated"] and row["oracle_strict_pass"] for row in rows)
    assert all((output / row["oracle_trajectory_path"]).is_file() for row in rows)
    assert all((output / row["task_path"]).is_file() and (output / row["assets_path"]).is_dir() for row in rows)
    assert len(bundle.inventory(output / "evidence")) == 132
    assert result["files"] == len(bundle.inventory(output))
    assert result["registry_digest"] == registry_digest
    identity["frozen_manifest_sha256"] = "0" * 64
    identity_path.write_bytes(json_bytes(identity))
    with pytest.raises(ValueError, match="wrong publication freeze"):
        bundle.prepare_hf(frozen, local, registry, harbor, identity_path, tmp_path / "wrong-hf")
