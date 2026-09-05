"""Synthetic API fixtures only; these tests never publish a benchmark."""
import hashlib
import json
from types import SimpleNamespace

import pytest
from huggingface_hub import CommitOperationAdd, CommitOperationDelete

from arc_release.build import DATASET, VERSION
from publication import hf
from publication.bundle import inventory, seal


def info(root, commit):
    siblings = [SimpleNamespace(rfilename=".gitattributes")]
    for entry in inventory(root):
        raw = (root / entry["path"]).read_bytes()
        blob = hashlib.sha1(f"blob {len(raw)}\0".encode() + raw, usedforsecurity=False).hexdigest()
        siblings.append(SimpleNamespace(rfilename=entry["path"], size=len(raw), blob_id=blob, lfs=None))
    return SimpleNamespace(id=hf.HF_REPO, sha=commit, private=False, gated=False, disabled=False, siblings=siblings)


@pytest.fixture
def upgrade(tmp_path):
    previous, current = tmp_path / "old", tmp_path / "new"
    for directory, version, filename in [(previous, "0.1.1", "old-input.tar"), (current, VERSION, "new-input.tar")]:
        directory.mkdir()
        (directory / "release.json").write_text(json.dumps({"dataset": DATASET, "version": version}))
        (directory / filename).write_bytes(b"synthetic fixture, not a real archive")
        seal(directory)
    old_commit, new_commit = "a" * 40, "b" * 40
    old_info, new_info = info(previous, old_commit), info(current, new_commit)
    receipt = tmp_path / "previous-receipt.json"
    receipt.write_text(json.dumps(hf.verify_objects(previous, old_info.siblings, old_commit)))

    class API:
        def __init__(self):
            self.head = old_commit
            self.versions = {old_commit: old_info, new_commit: new_info}
            self.writes = []
            self.race = False
            self.exists = True

        def repo_exists(self, *args, **kwargs):
            return self.exists

        def create_repo(self, *args, **kwargs):
            self.writes.append(("create", kwargs))

        def dataset_info(self, *args, revision=None, **kwargs):
            return self.versions[revision or self.head]

        def create_commit(self, **kwargs):
            self.writes.append(("upload", kwargs))
            assert kwargs["parent_commit"] == old_commit
            if self.race:
                raise RuntimeError("parent commit changed")
            self.head = new_commit
            return SimpleNamespace(oid=new_commit)

    return current, previous, receipt, API()


def publish(fixture):
    current, previous, receipt, api = fixture
    return hf.run(current, publish=True, previous_publication=previous, previous_receipt=receipt, api=api)


def test_patch_upload_uses_exact_parent_and_only_obsolete_paths(upgrade):
    receipt = publish(upgrade)
    api = upgrade[-1]
    assert len(api.writes) == 1
    operations = api.writes[0][1]["operations"]
    assert [op.path_in_repo for op in operations if isinstance(op, CommitOperationDelete)] == ["old-input.tar"]
    assert {op.path_in_repo for op in operations if isinstance(op, CommitOperationAdd)} == {
        "release.json", "publication-manifest.json", "new-input.tar",
    }
    assert api.writes[0][1]["revision"] == "main"
    assert api.writes[0][1]["create_pr"] is False
    assert receipt["previous_release"]["commit"] == "a" * 40
    assert receipt["previous_release"]["immutable_objects_preserved"] is True
    assert receipt["commit"] == "b" * 40
    assert api.versions["a" * 40].sha == "a" * 40


def test_exact_current_resume_preserves_old_without_another_write(upgrade):
    upgrade[-1].head = "b" * 40
    assert publish(upgrade)["previous_release"]["immutable_objects_preserved"] is True
    assert upgrade[-1].writes == []


@pytest.mark.parametrize("changed", ["receipt", "old-bytes", "old-remote", "head", "absent", "version"])
def test_unexpected_prior_state_fails_before_any_write(upgrade, changed):
    _, previous, receipt, api = upgrade
    if changed == "receipt":
        value = json.loads(receipt.read_text())
        value["payload_sha256"] = "0" * 64
        receipt.write_text(json.dumps(value))
    elif changed == "old-bytes":
        (previous / "old-input.tar").write_bytes(b"modified")
    elif changed == "old-remote":
        api.versions["a" * 40].siblings[1].size += 1
    elif changed == "head":
        api.head = "c" * 40
        api.versions[api.head] = info(previous, api.head)
    elif changed == "absent":
        api.exists = False
    else:
        (previous / "release.json").write_text(json.dumps({"dataset": DATASET, "version": "0.1.0"}))
        (previous / "publication-manifest.json").unlink()
        seal(previous)
    with pytest.raises(ValueError):
        publish(upgrade)
    assert api.writes == []


def test_race_during_upload_does_not_overwrite_moved_head(upgrade):
    api = upgrade[-1]
    api.race = True
    with pytest.raises(RuntimeError, match="parent commit changed"):
        publish(upgrade)
    assert api.head == "a" * 40
    assert len(api.writes) == 1


def test_updating_existing_repository_requires_both_previous_inputs(upgrade):
    current, previous, _, api = upgrade
    with pytest.raises(ValueError, match="both previous"):
        hf.run(current, publish=True, previous_publication=previous, api=api)
    with pytest.raises(ValueError):
        hf.run(current, publish=True, api=api)
    assert api.writes == []
