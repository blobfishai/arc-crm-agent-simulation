"""Local deterministic qualification, not a model evaluation or registry receipt."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from benchmark.hubbench.engine.families import SUBMIT_TOOL
from benchmark.hubbench.engine.validation import canonical_json
from benchmark.hubbench.engine.verifier import verify_episode
from benchmark.hubbench.engine.world import World

from . import FAMILY, SOURCE_LOCK, build_tasks
from .tasks import call


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def run(task: dict, steps: list[dict], database: Path) -> dict:
    if database.exists() or database.is_symlink():
        raise ValueError("qualification requires a new, isolated database path")
    with World.fresh(FAMILY, task, database) as world:
        initial = world.snapshot()
        for step in steps:
            world.call_tool(step["tool"], step["arguments"])
        return {
            "trace": world.trace,
            "verdict": verify_episode(task, world),
            "initial_state": initial,
            "final_state": world.snapshot(),
        }


def controls(task: dict) -> dict[str, list[dict]]:
    steps = deepcopy(task["oracle_steps"])
    first = next(
        i for i, step in enumerate(steps) if step["tool"] in FAMILY.write_tools
    )
    before, after = steps[:first], steps[first:]
    result = {
        "noop": [],
        "answer_only": [steps[-1]],
        "skip_investigation": after,
        "write_before_investigation": [steps[first], *before, *steps[first + 1 :]],
        "missing_readback": before
        + [step for step in after if step["tool"] in FAMILY.write_tools],
        "state_without_answer": steps[:-1],
    }
    # Each necessary investigation/readback is tested separately; each mutation
    # gets an omission episode, including note creation and predecessor cancellation.
    for index, step in enumerate(steps):
        if step["tool"] in FAMILY.write_tools - {SUBMIT_TOOL}:
            result[f"omit_mutation_{index}_{step['tool']}"] = (
                steps[:index] + steps[index + 1 :]
            )
        elif step["tool"] in FAMILY.read_tools and index > 0:
            result[f"omit_read_{index}_{step['tool']}"] = (
                steps[:index] + steps[index + 1 :]
            )
    stale = deepcopy(steps)
    stale[1] = call("desk.requests.get", request_id=f"RQ-{int(task['task_id'][-3:])}1")
    result["superseded_request"] = stale
    retired = deepcopy(steps)
    for step in retired:
        if step["tool"] == "desk.policies.get":
            step["arguments"]["policy_id"] = (
                step["arguments"]["policy_id"].removesuffix("-2") + "-1"
            )
    result["retired_policy"] = retired
    wrong_answer = deepcopy(steps)
    key = next(iter(wrong_answer[-1]["arguments"]))
    value = wrong_answer[-1]["arguments"][key]
    wrong_answer[-1]["arguments"][key] = (
        value + 1 if isinstance(value, int) else value + "-wrong"
    )
    result["wrong_answer"] = wrong_answer
    wrong_facts = deepcopy(steps)
    wrong_evidence = deepcopy(steps)
    for step in wrong_facts:
        if step["tool"] == "arccrm.notes.create":
            step["arguments"]["content"] = "Completed the requested changes."
    for step in wrong_evidence:
        if step["tool"] == "arccrm.notes.create":
            step["arguments"]["evidence_ids"] = [
                f"E-{int(task['task_id'][-3:])}-request-1"
            ]
    result["missing_literal_handoff_facts"] = wrong_facts
    result["stale_handoff_evidence"] = wrong_evidence
    for control in task["negative_controls"]:
        if control["mode"] == "extra_write":
            result[control["name"]] = before + [control["step"]] + after
        elif control["mode"] == "replace":
            result[control["name"]] = [
                call(step["tool"], **control["arguments"])
                if step["tool"] == control["tool"]
                else step
                for step in steps
            ]
        else:
            raise ValueError(f"unknown control mode: {control['mode']}")
    return result


def alternate_steps(task: dict) -> list[dict]:
    """A real non-oracle wording/order variant; no relaxation of sealed criteria."""
    steps = deepcopy(task["oracle_steps"])
    first = next(
        i for i, step in enumerate(steps) if step["tool"] in FAMILY.write_tools
    )
    steps[1:first] = reversed(steps[1:first])
    for step in steps:
        if step["tool"] == "arccrm.notes.create":
            facts = task["expected"]["assertions"][-1]["payload_argument_text"][
                "content"
            ]
            step["arguments"]["content"] = (
                "Operations handoff — verified records and facts: "
                + "; ".join(reversed(facts))
                + ". Please review the linked evidence."
            )
            step["arguments"]["reference_ids"].reverse()
            step["arguments"]["evidence_ids"].reverse()
    return steps


def qualify(tasks: list[dict] | None = None) -> dict:
    tasks = build_tasks() if tasks is None else tasks
    results = []
    with TemporaryDirectory(prefix="blobfish-arc-crm-qualification-") as temporary:
        root = Path(temporary)
        for task in tasks:
            first = run(
                task, task["oracle_steps"], root / f"{task['task_id']}-oracle.sqlite"
            )
            replay = run(
                task, task["oracle_steps"], root / f"{task['task_id']}-replay.sqlite"
            )
            alternate = run(
                task,
                alternate_steps(task),
                root / f"{task['task_id']}-alternate.sqlite",
            )
            negative = []
            for index, (name, steps) in enumerate(controls(task).items()):
                episode = run(
                    task, steps, root / f"{task['task_id']}-control-{index}.sqlite"
                )
                negative.append(
                    {
                        "name": name,
                        "strict_pass": episode["verdict"]["strict_pass"],
                        "score": episode["verdict"]["score"],
                        "trace_sha256": digest(episode["trace"]),
                        "verdict_sha256": digest(episode["verdict"]),
                    }
                )
            results.append(
                {
                    "task_id": task["task_id"],
                    "task_sha256": digest(task),
                    "oracle": first,
                    "replay_sha256": digest(replay),
                    "exact_replay": digest(first) == digest(replay),
                    "alternate_pass": alternate["verdict"]["strict_pass"]
                    and all(entry["success"] for entry in alternate["trace"]),
                    "alternate_trace": alternate["trace"],
                    "negative_controls": negative,
                }
            )
    passed = bool(results) and all(
        result["oracle"]["verdict"]["strict_pass"]
        and result["oracle"]["verdict"]["score"] == 100
        and all(entry["success"] for entry in result["oracle"]["trace"])
        and result["exact_replay"]
        and result["alternate_pass"]
        and all(not control["strict_pass"] for control in result["negative_controls"])
        for result in results
    )
    return {
        "adapter": SOURCE_LOCK["adapter"],
        "version": FAMILY.version,
        "source_lock_sha256": digest(SOURCE_LOCK),
        "scope": "local in-process oracle, deterministic replay and adversarial controls; no model, Docker, registry or publication claim",
        "engine_metric": "HubScore",
        "qualified": passed,
        "task_count": len(results),
        "oracle_passes": sum(
            result["oracle"]["verdict"]["strict_pass"] for result in results
        ),
        "negative_control_count": sum(
            len(result["negative_controls"]) for result in results
        ),
        "negative_false_accepts": sum(
            control["strict_pass"]
            for result in results
            for control in result["negative_controls"]
        ),
        "tasks": results,
    }
