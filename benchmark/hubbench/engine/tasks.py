"""Locate a family's tasks: from a release tree, a JSON path, or a fresh build."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .families import Family

HUBBENCH_ROOT = Path(__file__).resolve().parents[1]


def release_dir(family: Family) -> Path:
    return HUBBENCH_ROOT / "families" / family.slug / "release"


def load_release_tasks(family: Family, directory: Path | None = None) -> list[dict[str, Any]]:
    tasks_dir = (directory or release_dir(family)) / "tasks"
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(tasks_dir.glob(f"{family.slug}-*.json"))]


def load_release_contract(family: Family, task_id: str, directory: Path | None = None) -> dict[str, Any]:
    path = (directory or release_dir(family)) / "verifiers" / "contracts" / f"{task_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def load_task(family: Family, value: str, *, release_dir: Path | None = None) -> dict[str, Any]:
    """Resolve ``value`` as a task JSON path, a released task id, or a built task id."""

    path = Path(value)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    directory = release_dir or globals()["release_dir"](family)
    candidate = directory / "tasks" / f"{value}.json"
    if candidate.exists():
        return json.loads(candidate.read_text(encoding="utf-8"))
    for task in family.build_tasks():
        if task["task_id"] == value:
            return task
    raise FileNotFoundError(f"task {value!r} not found for family {family.slug}")


__all__ = ["HUBBENCH_ROOT", "load_release_contract", "load_release_tasks", "load_task", "release_dir"]
