import asyncio

import pytest

from publication import registry
from publication.bundle import TAG


def tags(**mapping):
    return [{"tag": name, "revision": revision} for name, revision in mapping.items()]


def test_exact_known_patch_upgrade_and_partial_resume():
    old = "v0.1.0"
    assert registry.transition(tags(**{old: 1, "latest": 1}), previous_tag=old) == "upgrade"
    assert registry.transition(tags(**{old: 1, TAG: 2, "latest": 2}), previous_tag=old) == "current"
    assert registry.transition(tags(**{TAG: 2, "latest": 2})) == "current"


@pytest.mark.parametrize("mapping,previous", [
    ({"v0.1.0": 1, "latest": 1}, None),
    ({"v0.1.0": 1, "latest": 3}, "v0.1.0"),
    ({"v0.1.0": 1, TAG: 2, "latest": 3}, "v0.1.0"),
    ({TAG: 2, "latest": 2}, "v0.1.0"),
    ({"v0.1.0": 2, TAG: 2, "latest": 2}, "v0.1.0"),
    ({"v0.1.0": 3, TAG: 2, "latest": 2}, "v0.1.0"),
    ({"v0.1.0": 1}, "v0.1.0"),
    ({TAG: 1, "latest": 1}, TAG),
    ({TAG: "1", "latest": "1"}, None),
    ({TAG: True, "latest": True}, None),
])
def test_unexpected_missing_moved_or_invalid_tags_stop_before_publication(mapping, previous):
    with pytest.raises(ValueError):
        registry.transition(tags(**mapping), previous_tag=previous)


def test_duplicate_registry_tags_fail_closed():
    values = tags(**{TAG: 1, "latest": 1})
    with pytest.raises(ValueError, match="duplicate"):
        registry.transition(values + [values[0]])


def test_old_tag_validation_can_omit_latest_but_not_its_version_tag():
    package = {"id": "package-identity", "type": "task", "visibility": "public", "org": {"name": "blobfishai"}}
    digest = "sha256:" + "a" * 64
    version = {"package_id": package["id"], "content_hash": digest, "tags": ["v0.1.0"]}
    registry.check_package(package, "task", digest, version, tag="v0.1.0", require_latest=False)
    with pytest.raises(ValueError, match="tags"):
        registry.check_package(package, "task", digest, version, tag="v0.1.0")
    with pytest.raises(ValueError, match="tags"):
        registry.check_package(package, "task", digest, version, require_latest=False)


@pytest.mark.parametrize("wrong", [None, "another-package"])
def test_version_is_bound_to_queried_package_without_a_name_field(wrong):
    package = {"id": "exact-package", "type": "task", "visibility": "public", "org": {"name": "blobfishai"}}
    digest = "sha256:" + "a" * 64
    version = {"package_id": "exact-package", "content_hash": digest, "tags": [TAG, "latest"]}
    registry.check_package(package, "task", digest, version)
    with pytest.raises(ValueError, match="different package"):
        registry.check_package(package, "task", digest, version | {"package_id": wrong})


def test_incomplete_previous_release_inputs_rejected(tmp_path):
    with pytest.raises(ValueError, match="all three"):
        registry.previous_release(tmp_path, None, None, "0.1.1")


def test_absent_target_is_only_available_for_first_publication(tmp_path):
    class AbsentDB:
        async def get_package(self, **kwargs):
            return None

    args = (AbsentDB(), "blobfishai/arc-crm-001", "task", tmp_path, tmp_path, [])
    assert asyncio.run(registry.inspect_target(*args, None)) == "new"
    with pytest.raises(ValueError, match="previous package disappeared"):
        asyncio.run(registry.inspect_target(*args, {"tag": "v0.1.0"}))


def test_upgrade_requires_old_immutable_metadata_verification(tmp_path, monkeypatch):
    class ExistingDB:
        async def get_package(self, **kwargs):
            return {"type": "task"}

        async def list_package_tags(self, **kwargs):
            return tags(**{"v0.1.0": 1, "latest": 1})

    inspected = []

    async def verify_previous(db, name, kind, previous, *, require_latest):
        inspected.append((name, kind, previous["tag"], require_latest))
        raise ValueError("old bytes differ")

    monkeypatch.setattr(registry, "verify_previous_remote", verify_previous)
    with pytest.raises(ValueError, match="old bytes differ"):
        asyncio.run(registry.inspect_target(ExistingDB(), "blobfishai/arc-crm-001", "task", tmp_path, tmp_path, [], {"tag": "v0.1.0"}))
    assert inspected == [("blobfishai/arc-crm-001", "task", "v0.1.0", True)]
