import json
from copy import deepcopy

import pytest

from benchmark.dataset_factory.adapters.arc_crm import FAMILY, SOURCE_LOCK, build_tasks
from benchmark.dataset_factory.adapters.arc_crm.qualification import (
    controls,
    digest,
    qualify,
    run,
)
from benchmark.dataset_factory.adapters.arc_crm.release import (
    freeze,
    public_task,
    verify_candidate,
    verify_source,
)
from benchmark.dataset_factory.adapters.arc_crm.runtime import (
    load_task,
    session_database,
)
from benchmark.dataset_factory.adapters.arc_crm.tools import SOURCE_NAMES
from benchmark.hubbench.engine.verifier import verify_episode
from benchmark.hubbench.engine.world import World


@pytest.fixture(scope="module")
def qualification():
    return qualify()


def test_scope_source_lock_and_deterministic_task_contracts():
    tasks = build_tasks()
    assert len(tasks) == len({task["workflow"] for task in tasks}) == 6
    assert len({task["world"]["name"] for task in tasks}) == 6
    assert tasks == build_tasks()
    assert len(FAMILY.tools) == 31
    assert len({name for names in SOURCE_NAMES.values() for name in names}) == 16
    assert SOURCE_LOCK["coverage"]["source_rows_reproduced"] == 0
    assert not SOURCE_LOCK["coverage"]["upstream_api_parity"]
    assert (
        SOURCE_LOCK["source"]["revision"] == "8efb61f5920812d03d6267ed25f4439acb82cfbf"
    )
    assert SOURCE_LOCK["source"]["review"]["populated_final_states"] == 0
    assert all(task["family"] == "arc-crm" for task in tasks)
    for task in tasks:
        public = public_task(task)
        assert (
            not {"expected", "oracle_steps", "seed_tables", "negative_controls"}
            & public.keys()
        )
        assert "source_rows_reproduced" in public["source_lineage"]
        assert sum(group["weight"] for group in task["rubric_milestones"]) == 100
        assert "newly created" in public["instruction"] or task["workflow"] not in {
            "unsigned_contract",
            "quote_replacement",
        }
    assert "CN-1" not in tasks[4]["instruction"]
    assert "Q-43" not in tasks[3]["instruction"]
    assert "C-11" not in tasks[0]["instruction"]
    assert "1350000" not in tasks[2]["instruction"]
    assert "1140000" not in tasks[3]["instruction"]
    assert "1876500" not in tasks[4]["instruction"]


def test_all_oracles_replays_variants_and_negative_controls(qualification):
    assert qualification["qualified"]
    assert qualification["oracle_passes"] == qualification["task_count"] == 6
    assert qualification["negative_control_count"] == 167
    assert qualification["negative_false_accepts"] == 0
    for result in qualification["tasks"]:
        assert result["oracle"]["verdict"]["score"] == 100
        assert all(entry["success"] for entry in result["oracle"]["trace"])
        assert result["exact_replay"] and result["alternate_pass"]
        assert result["replay_sha256"] == digest(result["oracle"])
        assert result["alternate_trace"] != result["oracle"]["trace"]
        assert all(
            not control["strict_pass"] for control in result["negative_controls"]
        )


@pytest.mark.parametrize("number", range(1, 7))
def test_each_domain_mutation_has_a_rejected_omission(number, qualification):
    task = load_task(f"arc-crm-{number:03d}")
    result = qualification["tasks"][number - 1]
    omissions = [
        control
        for control in result["negative_controls"]
        if control["name"].startswith("omit_mutation_")
    ]
    assert len(omissions) == task["write_count"]
    assert all(not control["strict_pass"] for control in omissions)


@pytest.fixture
def world(tmp_path):
    task = load_task("arc-crm-003")
    with World.fresh(FAMILY, task, tmp_path / "world.sqlite") as world:
        yield world


def write_step(task, tool):
    return next(deepcopy(step) for step in task["oracle_steps"] if step["tool"] == tool)


@pytest.mark.parametrize(
    "patch",
    [
        {"amount_minor": -1},
        {"amount_minor": True},
        {"amount_minor": 1.5},
        {"amount_minor": float("nan")},
        {"amount_minor": float("inf")},
        {"amount_minor": 10**20},
        {"currency": "EUR"},
        {"status": "signed"},
        {"valid_until": "2026-09-99"},
        {"valid_until": "2026-10-01"},
        {"valid_until": "2026-09-01"},
        {"approval_id": "AP-3-expired"},
        {"discount_bps": 1001},
        {"lines": []},
        {"lines": [{"sku": "SEAT", "quantity": 0}]},
        {"lines": [{"sku": "SEAT", "quantity": True}]},
        {"lines": [{"sku": "SEAT", "quantity": 10, "unit_minor": 1}]},
        {"lines": [{"sku": "SEAT", "quantity": 10}, {"sku": "SEAT", "quantity": 1}]},
        {"lines": [{"sku": "UNKNOWN", "quantity": 1}]},
        {"opportunity_id": "OP-32"},
        {"predecessor_id": "Q-32"},
        {"unknown_field": "not permitted"},
    ],
)
def test_quote_boundary_failures_roll_back_every_table(world, patch):
    step = write_step(world.task, "arccrm.quotes.create")
    initial = world.snapshot()
    result = world.call_tool(step["tool"], step["arguments"] | patch)
    assert "error" in result
    assert world.snapshot() == initial
    assert len(world.trace) == 1 and not world.trace[0]["success"]
    accepted = world.call_tool(step["tool"], step["arguments"])
    assert accepted["quote_id"] == "Q-33"  # failure did not consume an ID
    assert accepted["amount_minor"] == 1_350_000


def test_quote_price_math_uses_integer_half_up_not_float(world):
    world.connection.execute(
        "UPDATE crm_prices SET unit_minor=1001 WHERE sku='SEAT' AND revision=2"
    )
    world.connection.execute(
        "UPDATE crm_prices SET unit_minor=1 WHERE sku='SETUP' AND revision=2"
    )
    world.connection.commit()
    step = write_step(world.task, "arccrm.quotes.create")
    result = world.call_tool(step["tool"], step["arguments"] | {"amount_minor": 9010})
    assert result["gross_minor"] == 10011
    assert result["amount_minor"] == 9010
    assert {line["price_revision"] for line in result["lines"]} == {2}


def test_duplicate_open_quote_has_no_partial_lines_or_audit(world):
    step = write_step(world.task, "arccrm.quotes.create")
    assert "error" not in world.call_tool(step["tool"], step["arguments"])
    before = world.snapshot()
    assert "error" in world.call_tool(step["tool"], step["arguments"])
    assert world.snapshot() == before
    assert world.one("SELECT count(*) AS n FROM crm_quote_lines")["n"] == 2


@pytest.mark.parametrize(
    "number,tool,patch",
    [
        (1, "arccrm.contacts.create", {"client_id": "C-12"}),
        (1, "arccrm.contacts.create", {"email": "JAMIE@ASTER-FREIGHT.EXAMPLE"}),
        (
            1,
            "arccrm.contacts.create",
            {"email": "robin@aster-freight.example.evil.invalid"},
        ),
        (1, "arccrm.contacts.create", {"first_name": " "}),
        (1, "arccrm.contacts.create", {"title": "CEO"}),
        (2, "arccrm.opportunities.update", {"probability": 70}),
        (2, "arccrm.opportunities.update", {"expected_revision": 3}),
        (2, "arccrm.opportunities.update", {"amount_minor": 1}),
        (
            2,
            "arccrm.opportunities.update",
            {"opportunity_id": "Platform renewal - east"},
        ),
        (4, "arccrm.quotes.cancel", {"quote_id": "Q-41", "expected_version": 1}),
        (4, "arccrm.quotes.cancel", {"expected_version": 1}),
        (5, "arccrm.contracts.create", {"value_minor": 2_200_000}),
        (5, "arccrm.contracts.create", {"client_id": "C-52"}),
        (5, "arccrm.contracts.create", {"opportunity_id": "OP-52"}),
        (5, "arccrm.contracts.create", {"status": "signed"}),
        (5, "arccrm.contracts.create", {"signed_at": "2026-09-05"}),
        (
            6,
            "arccrm.documents.attach",
            {"document_url": "https://example.invalid/secret.pdf"},
        ),
        (6, "arccrm.documents.attach", {"document_url": "file:///etc/passwd"}),
        (
            6,
            "arccrm.documents.attach",
            {"document_url": "mock://crm/D-63", "file_name": "services-assessment.pdf"},
        ),
        (6, "arccrm.documents.attach", {"entity_type": "quote"}),
        (6, "arccrm.documents.attach", {"file_name": "../../secret"}),
        (
            6,
            "arccrm.documents.attach",
            {"document_url": "mock://crm/D-62", "file_name": "capacity-draft.pdf"},
        ),
    ],
)
def test_provider_scope_and_state_boundary(number, tool, patch, tmp_path):
    task = load_task(f"arc-crm-{number:03d}")
    step = write_step(task, tool)
    with World.fresh(FAMILY, task, tmp_path / "world.sqlite") as world:
        initial = world.snapshot()
        assert "error" in world.call_tool(tool, step["arguments"] | patch)
        assert world.snapshot() == initial


@pytest.mark.parametrize(
    "number,tool,target",
    [
        (1, "arccrm.contacts.create", "C-11"),
        (3, "arccrm.quotes.create", "OP-31"),
        (5, "arccrm.contracts.create", "Q-52"),
        (6, "arccrm.documents.attach", "OP-61"),
    ],
)
def test_expired_grants_do_not_authorize_writes(number, tool, target, tmp_path):
    task = load_task(f"arc-crm-{number:03d}")
    with World.fresh(FAMILY, task, tmp_path / "world.sqlite") as world:
        world.connection.execute(
            "UPDATE crm_permissions SET valid_until='2026-08-31' WHERE target_id=?",
            (target,),
        )
        world.connection.commit()
        before = world.snapshot()
        result = world.call_tool(tool, write_step(task, tool)["arguments"])
        assert "no current permission" in result["error"]
        assert world.snapshot() == before


def test_unsupported_deletion_and_sql_queries_cannot_change_history(tmp_path):
    task = load_task("arc-crm-004")
    with World.fresh(FAMILY, task, tmp_path / "world.sqlite") as world:
        before = world.snapshot()
        assert (
            "unknown tool"
            in world.call_tool("arccrm.quotes.delete", {"quote_id": "Q-41"})["error"]
        )
        assert (
            world.call_tool("arccrm.clients.search", {"query": "' OR 1=1 --"})["total"]
            == 0
        )
        assert world.call_tool("arccrm.clients.search", {"query": "%"})["total"] == 0
        assert world.snapshot() == before


@pytest.mark.parametrize(
    "number,query",
    [
        (1, "UPDATE crm_clients SET name='wrong' WHERE client_id='C-12'"),
        (2, "UPDATE crm_opportunities SET amount_minor=1 WHERE opportunity_id='OP-22'"),
        (3, "UPDATE crm_quote_lines SET quantity=999 WHERE quote_id='Q-33'"),
        (4, "DELETE FROM crm_quote_lines WHERE quote_id='Q-41'"),
        (5, "UPDATE crm_contracts SET quote_id='Q-51' WHERE contract_id='CN-1'"),
        (6, "DELETE FROM crm_attachments WHERE attachment_id='AT-1'"),
    ],
)
def test_unaudited_wrong_rows_are_rejected_independently(number, query, tmp_path):
    task = load_task(f"arc-crm-{number:03d}")
    database = tmp_path / "world.sqlite"
    assert run(task, task["oracle_steps"], database)["verdict"]["strict_pass"]
    with World(FAMILY, task, database) as world:
        world.connection.execute(query)
        world.connection.commit()
        assert not verify_episode(task, world)["strict_pass"]


def test_a_permitted_wrong_record_is_not_accepted_as_task_success(tmp_path):
    task = load_task("arc-crm-002")
    steps = controls(task)["wrong_authorized_entity"]
    episode = run(task, steps, tmp_path / "world.sqlite")
    wrong = next(
        entry
        for entry in episode["trace"]
        if entry["tool"] == "arccrm.opportunities.update"
    )
    assert wrong["success"] and wrong["result"]["opportunity_id"] == "OP-22"
    assert not episode["verdict"]["strict_pass"]


def test_note_cannot_claim_an_absent_or_foreign_record(tmp_path):
    task = load_task("arc-crm-006")
    step = write_step(task, "arccrm.notes.create")
    with World.fresh(FAMILY, task, tmp_path / "world.sqlite") as world:
        before = world.snapshot()
        assert "error" in world.call_tool(step["tool"], step["arguments"])
        assert "error" in world.call_tool(
            step["tool"], step["arguments"] | {"reference_ids": ["AT-1"]}
        )
        assert world.snapshot() == before


@pytest.mark.parametrize(
    "query,passes",
    [("OP-51", True), ("", True), ("OP-52", False), ("unrelated", False)],
)
def test_empty_contract_search_must_cover_the_intended_opportunity(
    query, passes, tmp_path
):
    task = load_task("arc-crm-005")
    steps = deepcopy(task["oracle_steps"])
    for step in steps:
        if step["tool"] == "arccrm.contracts.search":
            step["arguments"] = {"query": query}
    assert (
        run(task, steps, tmp_path / "world.sqlite")["verdict"]["strict_pass"] is passes
    )


def test_session_is_bound_and_never_implicitly_overwritten(tmp_path):
    task = load_task("arc-crm-001")
    folder = tmp_path / "episode"
    database = session_database(folder, task)
    with World(FAMILY, task, database) as world:
        world.call_tool("hubbench.context.get", {})
    before = database.read_bytes()
    assert session_database(folder, task) == database
    with pytest.raises(ValueError, match="does not match"):
        session_database(folder, load_task("arc-crm-002"))
    assert database.read_bytes() == before
    altered = deepcopy(task)
    altered["instruction"] += " changed"
    with pytest.raises(ValueError, match="does not match"):
        session_database(folder, altered)
    identity = json.loads((folder / "identity.json").read_text())
    identity["implementation_sha256"] = "wrong"
    (folder / "identity.json").write_text(json.dumps(identity))
    with pytest.raises(ValueError, match="does not match"):
        session_database(folder, task)
    assert database.read_bytes() == before


def test_unowned_and_symlinked_sessions_are_rejected_without_touching_data(tmp_path):
    task = load_task("arc-crm-001")
    unowned = tmp_path / "unowned"
    unowned.mkdir()
    sentinel = unowned / "valuable.txt"
    sentinel.write_text("preserve me")
    with pytest.raises(ValueError, match="unowned"):
        session_database(unowned, task)
    linked = tmp_path / "linked"
    linked.symlink_to(unowned, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        session_database(linked, task)
    partial = tmp_path / "partial"
    partial.mkdir()
    (partial / "world.sqlite").symlink_to(sentinel)
    with pytest.raises(ValueError, match="regular owned"):
        session_database(partial, task)
    assert sentinel.read_text() == "preserve me"


def test_source_verification_refuses_unpinned_bytes(tmp_path):
    (tmp_path / "README.md").write_text("not the pinned source")
    with pytest.raises(ValueError, match="source hash/size mismatch"):
        verify_source(tmp_path)


def test_freeze_is_exact_repeatable_and_not_a_publication_claim(tmp_path):
    left, right = tmp_path / "left", tmp_path / "right"
    first, second = freeze(left), freeze(right)
    assert first == second == verify_candidate(left)
    assert first["task_count"] == 6 and first["tool_count"] == 31
    assert first["status"] == "local-candidate" and not first["publication_ready"]
    assert first["source_rows_reproduced"] == 0
    for file in first["files"]:
        assert (left / file["path"]).read_bytes() == (right / file["path"]).read_bytes()
    pdf = next(file for file in first["files"] if file["path"].endswith(".pdf"))
    assert (left / pdf["path"]).read_bytes().startswith(b"%PDF-")
    xlsx = next(file for file in first["files"] if file["path"].endswith(".xlsx"))
    assert (left / xlsx["path"]).read_bytes().startswith(b"PK")
    with pytest.raises(ValueError, match="never overwritten"):
        freeze(left)
    (left / pdf["path"]).write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="hash mismatch"):
        verify_candidate(left)


def test_empty_qualification_is_not_success():
    assert not qualify([])["qualified"]
