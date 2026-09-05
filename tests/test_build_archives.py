import io
import json
import os
import re
import shutil
import tarfile

import pytest
from test_packages import frozen as frozen  # explicit fixture re-export

from arc_release import build


@pytest.mark.parametrize("number", range(1, 7))
def test_every_image_archive_exactly_matches_its_minimal_projection(frozen, number):
    task = frozen / "harbor/tasks" / f"arc-crm-{number:03}"
    mappings = [
        ("environment/Dockerfile", [("environment/client", "opt/arc-client"),
                                    ("environment/public", "public"), ("environment/tool", "usr/local/bin/tool")]),
        ("environment/Dockerfile.world", [("environment/world", "opt/arc-world")]),
        ("tests/Dockerfile", [("tests", "tests")]),
    ]
    for dockerfile, inputs in mappings:
        definition = (task / dockerfile).read_text()
        name, digest = re.search(r"COPY (inputs-([a-f0-9]{64})\.tar) /tmp/", definition).groups()
        raw = (task / dockerfile).parent.joinpath(name).read_bytes()
        assert build.sha(raw) == digest
        assert f'{digest}  /tmp/{name}' in definition and "sha256sum --check --strict" in definition
        assert definition.count("COPY ") == 1 and "ADD " not in definition
        expected = {}
        for original, destination in inputs:
            source = task / original
            for path in sorted(source.rglob("*")) if source.is_dir() else [source]:
                if path.is_file() and path.name != "Dockerfile" and not path.name.startswith("inputs-"):
                    relative = f"{destination}/{path.relative_to(source).as_posix()}" if source.is_dir() else destination
                    expected[relative] = path.read_bytes()
        with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
            members = archive.getmembers()
            assert all(member.isfile() and member.mtime == 0 and member.uid == member.gid == 0
                       and member.mode == 0o644 for member in members)
            actual = {member.name: archive.extractfile(member).read() for member in members}
            assert len(actual) == len(members)
        assert actual == expected


def test_all_eighteen_build_inputs_have_distinct_content_names(frozen):
    paths = list((frozen / "harbor/tasks").rglob("inputs-*.tar"))
    assert len(paths) == len({path.name for path in paths}) == 18


def test_archive_is_deterministic_and_changes_for_equal_length_metadata():
    first = {"task.json": b"same", "identity.json": b'{"task":"001"}'}
    second = first | {"identity.json": b'{"task":"003"}'}
    assert build.archive_bytes(first) == build.archive_bytes(dict(reversed(list(first.items()))))
    assert len(build.archive_bytes(first)) == len(build.archive_bytes(second))
    assert build.sha(build.archive_bytes(first)) != build.sha(build.archive_bytes(second))


@pytest.mark.parametrize("path", ["/task", "../task", "a/../task", "a\\task"])
def test_build_archive_rejects_unsafe_member_paths(path):
    with pytest.raises(ValueError, match="unsafe release member"):
        build.archive_bytes({path: b"invalid"})


@pytest.mark.parametrize("name", ["inputs.tar", "inputs-latest.tar", "../inputs-" + "a" * 64 + ".tar"])
def test_dockerfile_requires_content_addressed_archive_name(name):
    with pytest.raises(ValueError, match="content-addressed"):
        build.install_archive(name)


def test_registry_style_epoch_timestamps_preserve_every_frozen_identity(frozen, tmp_path):
    assert all(path.stat().st_mtime_ns == 0 for path in frozen.rglob("*") if path.is_file())
    target = tmp_path / "epoch-mtime"
    shutil.copytree(frozen, target)
    for path in target.rglob("*"):
        os.utime(path, ns=(0, 0), follow_symlinks=False)
    assert build.verify_freeze(target) == build.verify_freeze(frozen)


def test_historical_freeze_is_checked_against_its_own_version(frozen, monkeypatch):
    version = json.loads((frozen / "manifest.json").read_text())["version"]
    monkeypatch.setattr(build, "VERSION", "0.1.99")
    assert build.verify_freeze(frozen)["version"] == version
