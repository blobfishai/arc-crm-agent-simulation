import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from arc_release.build import (
    DATASET,
    SEALED_KEYS,
    content_hash,
    freeze,
    source_identity,
    verify_freeze,
)


@pytest.fixture(scope="session")
def frozen(tmp_path_factory):
    root = tmp_path_factory.mktemp("arc-packages") / "freeze"
    freeze(root)
    return root


def test_frozen_membership_identities_and_card(frozen):
    manifest = verify_freeze(frozen)
    assert manifest["dataset"] == DATASET
    assert manifest["task_count"] == 6
    assert manifest["source_rows_reproduced"] == 0
    assert manifest["publication_ready"] is False
    assert manifest["model_evaluated"] is False
    assert len(manifest["files"]) > 400
    records = [json.loads(line) for line in (frozen / "data/tasks.jsonl").read_text().splitlines()]
    assert len(records) == 6
    assert all(record["model_evaluated"] is False and "seed_tables" not in record for record in records)
    assert "zero source rows are reproduced" in (frozen / "README.md").read_text()


@pytest.mark.parametrize("number", range(1, 7))
def test_package_has_three_minimal_separate_inputs(frozen, number, tmp_path):
    root = frozen / "harbor/tasks" / f"arc-crm-{number:03}"
    config = tomllib.loads((root / "task.toml").read_text())
    assert config["agent"]["user"] == "10001" and config["verifier"]["user"] == "0"
    assert config["verifier"]["environment_mode"] == "separate"
    assert (root / "tests/Dockerfile").is_file()
    assert "network_mode: none" in (root / "tests/docker-compose.yaml").read_text()
    assert config["verifier"]["collect"][0]["service"] == "world"
    assert config["environment"]["cpus"] == 1 and config["environment"]["gpus"] == 0
    assert len(config["environment"]["mcp_servers"]) == 4
    assert "boundary.py health" in config["environment"]["healthcheck"]["command"]
    dockerfile = (root / "environment/Dockerfile").read_text()
    assert "COPY world" not in dockerfile and "COPY tests" not in dockerfile and "COPY solution" not in dockerfile
    assert "COPY client/" in dockerfile and "COPY public/" in dockerfile
    compose = (root / "environment/docker-compose.yaml").read_text()
    assert "internal: true" in compose and "ports:" not in compose and "external:" not in compose
    assert "cpus: 0.5" in compose and "mem_limit: 512m" in compose and "pids_limit: 128" in compose
    assert "arc-control:" not in compose.split("  world:")[0]
    public = json.loads((root / "environment/public/task.json").read_text())
    world = json.loads((root / "environment/world/task.json").read_text())
    sealed = json.loads((root / "tests/task.json").read_text())
    for private in ["expected", "oracle_steps", "negative_controls", "required_investigations", "post_write_verifications", "rubric_milestones"]:
        assert private not in public and private not in world
    assert "seed_tables" not in public and "seed_tables" in world and "seed_tables" not in sealed
    assert set(sealed) == set(SEALED_KEYS)
    assert not list((root / "environment").rglob("verifier.py"))
    assert not list((root / "environment").rglob("qualification.py"))
    assert not list((root / "environment/client").rglob("benchmark"))
    assert not list((root / "tests").rglob("arc_world"))
    assert len(json.loads((root / "environment/public/tools.json").read_text())) == 33
    # Import only each copied closure, from outside either checkout, ignoring
    # PYTHONPATH/site-packages. No authoring adapter can leak in transitively.
    for relative, script in [
        ("environment/client", "import client; import boundary; assert 'benchmark' not in sys.modules"),
        ("environment/world/runtime", "from arc_world import FAMILY; import benchmark.hubbench.engine.http; assert len(FAMILY.tools)==31; assert not any('verifier' in name or 'dataset_factory' in name for name in sys.modules)"),
        ("tests/runtime", "import benchmark.hubbench.engine.verifier; assert not any('arc_world' in name or 'dataset_factory' in name for name in sys.modules)"),
    ]:
        completed = subprocess.run(
            [sys.executable, "-I", "-S", "-B", "-c", "import sys; sys.path.insert(0, sys.argv[1]); " + script, str(root / relative)],
            cwd=tmp_path, capture_output=True, text=True, timeout=10,
        )
        assert completed.returncode == 0, completed.stderr


def test_hash_matches_installed_harbor_for_every_task(frozen):
    harbor_python = Path("/Users/samuelchien/.local/share/uv/tools/harbor/bin/python")
    if not harbor_python.exists():
        pytest.skip("optional installed-Harbor cross-check; run in publication environment")
    script = """
import json, sys
from pathlib import Path
from harbor.publisher.packager import Packager
from harbor.models.task.config import TaskConfig
from harbor.models.dataset.manifest import DatasetManifest
import tomllib
root = Path(sys.argv[1])
DatasetManifest.model_validate(tomllib.loads((root / 'dataset.toml').read_text()))
result = {}
for task in sorted((root / 'tasks').iterdir()):
    TaskConfig.model_validate(tomllib.loads((task / 'task.toml').read_text()))
    value, files = Packager.compute_content_hash(task)
    result[task.name] = {'digest': 'sha256:' + value, 'files': len(files)}
print(json.dumps(result))
"""
    completed = subprocess.run([str(harbor_python), "-c", script, str(frozen / "harbor")], capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    actual = json.loads(completed.stdout)
    for reference in verify_freeze(frozen)["tasks"]:
        task_id = reference["name"].split("/")[1]
        assert actual[task_id] == {"digest": reference["digest"], "files": reference["files"]}


def test_hash_includes_pyo_but_not_pyc_and_rejects_custom_ignores(tmp_path):
    task = tmp_path / "task"
    environment = task / "environment"
    environment.mkdir(parents=True)
    (task / "task.toml").write_text("x")
    before, _ = content_hash(task)
    (environment / "cache.pyc").write_bytes(b"cache")
    assert content_hash(task)[0] == before
    (environment / "cache.pyo").write_bytes(b"part of package")
    assert content_hash(task)[0] != before
    (task / ".gitignore").write_text("*")
    with pytest.raises(ValueError, match="custom"):
        content_hash(task)


def test_refuses_overwrite_and_dirty_publication(frozen, monkeypatch):
    with pytest.raises(ValueError, match="never overwrite"):
        freeze(frozen)
    monkeypatch.setattr(subprocess, "check_output", lambda command, **_: "a" * 40 if "rev-parse" in command else "?? dirty.py\n")
    with pytest.raises(ValueError, match="clean"):
        source_identity(require_clean=True)


def test_manifest_rejects_changed_membership_and_bytes(frozen, tmp_path):
    import shutil

    root = tmp_path / "altered"
    shutil.copytree(frozen, root)
    asset = next((root / "tasks").rglob("*.pdf"))
    original = asset.read_bytes()
    asset.write_bytes(original + b"changed")
    with pytest.raises(ValueError, match="identity changed"):
        verify_freeze(root)
    asset.write_bytes(original)
    (root / "unexpected.txt").write_text("unlisted")
    with pytest.raises(ValueError, match="membership"):
        verify_freeze(root)
    os.unlink(root / "unexpected.txt")
    assert verify_freeze(root)["task_count"] == 6
