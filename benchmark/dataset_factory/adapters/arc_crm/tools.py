"""Independent CRM contracts. Source names below indicate inspiration, not ABI parity."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from benchmark.hubbench.engine.families import ToolSpec
from benchmark.hubbench.engine.validation import canonical_json, integer, obj, string
from benchmark.hubbench.engine.world import World

ID = string(minLength=1, pattern=r"^[A-Za-z0-9-]+$")
TEXT = string(minLength=1)
MONEY = integer(minimum=0, maximum=1_000_000_000_000)
SOURCE_NAMES: dict[str, list[str]] = {
    "arccrm.clients.search": ["client_search"],
    "arccrm.contacts.search": ["contact_search"],
    "arccrm.contacts.create": ["create_new_contact"],
    "arccrm.opportunities.search": ["opportunity_search"],
    "arccrm.opportunities.get": ["view_opportunity_details", "opportunity_details"],
    "arccrm.opportunities.update": ["modify_opportunity"],
    "arccrm.quotes.search": ["quote_search"],
    "arccrm.quotes.get": ["quote_details"],
    "arccrm.quotes.create": ["create_quote"],
    "arccrm.quotes.cancel": ["cancel_quote"],
    "arccrm.quotes.compare": ["compare_quotes"],
    "arccrm.contracts.search": ["contract_search"],
    "arccrm.contracts.create": ["create_contract"],
    "arccrm.documents.attach": ["upload_document"],
    "arccrm.notes.create": ["add_note"],
}


def record(world: World, table: str, key: str, value: str) -> dict[str, Any]:
    # Table/column names are constants from handlers, never user-supplied SQL.
    return world.one(f"SELECT * FROM {table} WHERE {key} = ?", (value,))


def active_client(world: World, client_id: str) -> dict[str, Any]:
    client = record(world, "crm_clients", "client_id", client_id)
    if client["status"] != "active":
        raise ValueError("client is not active")
    return client


def permission(world: World, operation: str, target: str) -> dict[str, Any]:
    grant = world.one(
        "SELECT * FROM crm_permissions WHERE user_id = ? AND operation = ? AND target_id = ? "
        "AND valid_from <= ? AND valid_until >= ?",
        (
            world.task["role"],
            operation,
            target,
            world.as_of.isoformat(),
            world.as_of.isoformat(),
        ),
        missing=f"no current permission for {operation} on {target}",
    )
    return grant


def changed(
    world: World,
    tool: str,
    table: str,
    key: str,
    row: dict[str, Any],
    args: dict[str, Any],
    status: str,
) -> dict[str, Any]:
    world.audit(tool, table, row[key], status, args)
    mutation = world.record_mutation(tool, table, row[key], status, args)
    return {**row, "mutation_id": mutation}


def search(
    world: World, args: dict[str, Any], table: str, columns: tuple[str, ...], order: str
) -> dict[str, Any]:
    # Literal, case-insensitive substring search: no SQL wildcard interpretation.
    query = args.get("query", "").strip().casefold()
    where = " OR ".join(f"instr(lower({column}), ?) > 0" for column in columns)
    rows = world.all(
        f"SELECT * FROM {table} WHERE {where} ORDER BY {order}", [query] * len(columns)
    )
    return {"query": query, "records": rows, "total": len(rows)}


def get_policy(world: World, args: dict[str, Any]) -> dict[str, Any]:
    row = record(world, "crm_policies", "policy_id", args["policy_id"])
    return {
        **row,
        "rules": json.loads(row["rules_json"]),
        "operative": row["effective_from"]
        <= world.as_of.isoformat()
        <= row["effective_until"],
    }


def create_contact(world: World, args: dict[str, Any]) -> dict[str, Any]:
    client = active_client(world, args["client_id"])
    permission(world, "contact:create", client["client_id"])
    email = args["email"].strip().casefold()
    if (
        email.count("@") != 1
        or email.split("@")[1] != client["domain"].casefold()
        or any(c.isspace() for c in email)
    ):
        raise ValueError("email must use the verified client domain")
    if not all(args[field].strip() for field in ("first_name", "last_name")):
        raise ValueError("contact names cannot be blank")
    row = {
        "contact_id": world.next_id("crm_contacts", "contact_id", "CT-"),
        "client_id": client["client_id"],
        "first_name": args["first_name"].strip(),
        "last_name": args["last_name"].strip(),
        "email": email,
        "title": "",
        "revision": 1,
    }
    world.connection.execute(
        "INSERT INTO crm_contacts VALUES (?,?,?,?,?,?,?)", tuple(row.values())
    )
    return changed(
        world,
        "arccrm.contacts.create",
        "crm_contacts",
        "contact_id",
        row,
        args,
        "created",
    )


def update_opportunity(world: World, args: dict[str, Any]) -> dict[str, Any]:
    old = record(world, "crm_opportunities", "opportunity_id", args["opportunity_id"])
    active_client(world, old["client_id"])
    permission(world, "opportunity:update", old["opportunity_id"])
    rules = world.one(
        "SELECT * FROM crm_policies WHERE topic = 'stage' AND effective_from <= ? AND effective_until >= ? ORDER BY revision DESC LIMIT 1",
        (world.as_of.isoformat(),) * 2,
    )
    pair = json.loads(rules["rules_json"])["probability_by_stage"]
    if pair.get(args["stage"]) != args["probability"]:
        raise ValueError("stage/probability pair violates the operative policy")
    if old["revision"] != args["expected_revision"]:
        raise ValueError("stale opportunity revision; read the current record")
    if old["stage"] in {"won", "lost"}:
        raise ValueError("closed opportunities cannot be changed by this role")
    world.connection.execute(
        "UPDATE crm_opportunities SET stage=?, probability=?, revision=revision+1 WHERE opportunity_id=?",
        (args["stage"], args["probability"], old["opportunity_id"]),
    )
    row = record(world, "crm_opportunities", "opportunity_id", old["opportunity_id"])
    return changed(
        world,
        "arccrm.opportunities.update",
        "crm_opportunities",
        "opportunity_id",
        row,
        args,
        "updated",
    )


def get_quote(world: World, args: dict[str, Any]) -> dict[str, Any]:
    row = record(world, "crm_quotes", "quote_id", args["quote_id"])
    return {
        **row,
        "lines": world.all(
            "SELECT * FROM crm_quote_lines WHERE quote_id=? ORDER BY sku",
            (row["quote_id"],),
        ),
    }


def create_quote(world: World, args: dict[str, Any]) -> dict[str, Any]:
    opp = record(world, "crm_opportunities", "opportunity_id", args["opportunity_id"])
    active_client(world, opp["client_id"])
    grant = permission(world, "quote:create", opp["opportunity_id"])
    approval = record(world, "crm_approvals", "approval_id", args["approval_id"])
    until = date.fromisoformat(args["valid_until"]).isoformat()
    if (
        args["status"] != "draft"
        or approval["status"] != "approved"
        or approval["opportunity_id"] != opp["opportunity_id"]
        or args["currency"] != opp["currency"]
        or args["currency"] != approval["currency"]
        or not world.as_of.isoformat() <= until <= approval["expires_on"]
        or args["discount_bps"] > approval["max_discount_bps"]
    ):
        raise ValueError(
            "quote violates draft status, currency, approval scope, discount, or expiry"
        )
    if opp["stage"] in {"won", "lost"}:
        raise ValueError("cannot quote a closed opportunity")
    if (
        not args["lines"]
        or len(args["lines"]) > 50
        or len({line["sku"] for line in args["lines"]}) != len(args["lines"])
    ):
        raise ValueError("quote needs 1-50 distinct SKUs")
    lines = []
    for line in args["lines"]:
        price = world.one(
            "SELECT * FROM crm_prices WHERE sku=? AND currency=? AND valid_from<=? AND valid_until>=? ORDER BY revision DESC LIMIT 1",
            (
                line["sku"],
                args["currency"],
                world.as_of.isoformat(),
                world.as_of.isoformat(),
            ),
            missing="no operative price for SKU/currency",
        )
        lines.append(
            {
                "sku": line["sku"],
                "quantity": line["quantity"],
                "unit_minor": price["unit_minor"],
                "price_revision": price["revision"],
            }
        )
    gross = sum(line["quantity"] * line["unit_minor"] for line in lines)
    # Single invoice-level rounding, half up, in integer minor units.
    amount = (gross * (10_000 - args["discount_bps"]) + 5_000) // 10_000
    if (
        gross > approval["max_gross_minor"]
        or amount > grant["limit_minor"]
        or amount != args["amount_minor"]
    ):
        raise ValueError("quote total is incorrect or exceeds approval/role limits")
    predecessor = args.get("predecessor_id")
    if predecessor:
        prior = record(world, "crm_quotes", "quote_id", predecessor)
        if (
            prior["opportunity_id"] != opp["opportunity_id"]
            or prior["status"] != "cancelled"
        ):
            raise ValueError(
                "replacement predecessor must be a cancelled quote on the same opportunity"
            )
    version = world.one(
        "SELECT coalesce(max(version), 0)+1 AS n FROM crm_quotes WHERE opportunity_id=?",
        (opp["opportunity_id"],),
    )["n"]
    row = {
        "quote_id": world.next_id("crm_quotes", "quote_id", "Q-"),
        "opportunity_id": opp["opportunity_id"],
        "amount_minor": amount,
        "currency": args["currency"],
        "discount_bps": args["discount_bps"],
        "status": "draft",
        "valid_until": until,
        "version": version,
        "predecessor_id": predecessor,
        "approval_id": approval["approval_id"],
    }
    world.connection.execute(
        "INSERT INTO crm_quotes VALUES (?,?,?,?,?,?,?,?,?,?)", tuple(row.values())
    )
    for line in lines:
        world.connection.execute(
            "INSERT INTO crm_quote_lines VALUES (?,?,?,?,?)",
            (row["quote_id"], *line.values()),
        )
    world.audit(
        "arccrm.quotes.create",
        "crm_quote_lines",
        row["quote_id"],
        "created",
        {"lines": lines},
    )
    return {
        **changed(
            world,
            "arccrm.quotes.create",
            "crm_quotes",
            "quote_id",
            row,
            args,
            "created",
        ),
        "lines": lines,
        "gross_minor": gross,
    }


def cancel_quote(world: World, args: dict[str, Any]) -> dict[str, Any]:
    row = record(world, "crm_quotes", "quote_id", args["quote_id"])
    permission(world, "quote:cancel", row["quote_id"])
    if row["status"] != "draft" or row["version"] != args["expected_version"]:
        raise ValueError(
            "only the expected draft quote version may be cancelled; history is never deleted"
        )
    world.connection.execute(
        "UPDATE crm_quotes SET status='cancelled' WHERE quote_id=?", (row["quote_id"],)
    )
    row["status"] = "cancelled"
    return changed(
        world, "arccrm.quotes.cancel", "crm_quotes", "quote_id", row, args, "cancelled"
    )


def compare_quotes(world: World, args: dict[str, Any]) -> dict[str, Any]:
    ids = args["quote_ids"]
    if len(ids) < 2 or len(ids) > 20 or len(ids) != len(set(ids)):
        raise ValueError("compare 2-20 distinct quotes")
    rows = [get_quote(world, {"quote_id": value}) for value in sorted(ids)]
    if len({row["currency"] for row in rows}) != 1:
        raise ValueError("cannot compare quote amounts across currencies")
    return {
        "quotes": rows,
        "spread_minor": max(row["amount_minor"] for row in rows)
        - min(row["amount_minor"] for row in rows),
    }


def create_contract(world: World, args: dict[str, Any]) -> dict[str, Any]:
    quote = record(world, "crm_quotes", "quote_id", args["quote_id"])
    opp = record(world, "crm_opportunities", "opportunity_id", args["opportunity_id"])
    active_client(world, args["client_id"])
    grant = permission(world, "contract:create", quote["quote_id"])
    if (
        args["status"] != "draft"
        or quote["status"] != "approved"
        or quote["valid_until"] < world.as_of.isoformat()
        or quote["opportunity_id"] != opp["opportunity_id"]
        or opp["client_id"] != args["client_id"]
        or args["value_minor"] != quote["amount_minor"]
        or args["currency"] != quote["currency"]
        or args["value_minor"] > grant["limit_minor"]
    ):
        raise ValueError(
            "contract must be an unsigned draft matching a current approved quote and authorized client/value"
        )
    row = {
        "contract_id": world.next_id("crm_contracts", "contract_id", "CN-"),
        "client_id": args["client_id"],
        "opportunity_id": args["opportunity_id"],
        "quote_id": args["quote_id"],
        "value_minor": args["value_minor"],
        "currency": args["currency"],
        "status": "draft",
        "signed_at": None,
    }
    world.connection.execute(
        "INSERT INTO crm_contracts VALUES (?,?,?,?,?,?,?,?)", tuple(row.values())
    )
    return changed(
        world,
        "arccrm.contracts.create",
        "crm_contracts",
        "contract_id",
        row,
        args,
        "created",
    )


def attach_document(world: World, args: dict[str, Any]) -> dict[str, Any]:
    # Exact lookup only. No network, URL resolving, local path opening, or uploads.
    doc = record(world, "crm_documents", "url", args["document_url"])
    opp = record(world, "crm_opportunities", "opportunity_id", args["entity_id"])
    active_client(world, opp["client_id"])
    permission(world, "document:attach", opp["opportunity_id"])
    if (
        args["entity_type"] != "opportunity"
        or doc["client_id"] != opp["client_id"]
        or doc["status"] != "approved"
        or doc["file_name"] != args["file_name"]
    ):
        raise ValueError(
            "document must be the approved seeded file for the same client and opportunity entity type"
        )
    row = {
        "attachment_id": world.next_id("crm_attachments", "attachment_id", "AT-"),
        "document_id": doc["document_id"],
        "opportunity_id": opp["opportunity_id"],
        "file_name": doc["file_name"],
    }
    world.connection.execute(
        "INSERT INTO crm_attachments VALUES (?,?,?,?)", tuple(row.values())
    )
    return changed(
        world,
        "arccrm.documents.attach",
        "crm_attachments",
        "attachment_id",
        row,
        args,
        "attached",
    )


def create_note(world: World, args: dict[str, Any]) -> dict[str, Any]:
    entity_type, target = args["entity_type"], args["entity_id"]
    if entity_type == "client":
        client = active_client(world, target)
    else:
        client = active_client(
            world,
            record(world, "crm_opportunities", "opportunity_id", target)["client_id"],
        )
    permission(world, "note:create", target)
    if (
        not args["content"].strip()
        or not args["reference_ids"]
        or not args["evidence_ids"]
    ):
        raise ValueError("handoff needs nonempty text, references, and evidence")
    if len(args["reference_ids"]) != len(set(args["reference_ids"])) or len(
        args["evidence_ids"]
    ) != len(set(args["evidence_ids"])):
        raise ValueError("handoff references and evidence must be distinct")
    for ref in args["reference_ids"]:
        matches = world.all(
            "SELECT client_id, NULL AS opportunity_id FROM crm_contacts WHERE contact_id=? "
            "UNION ALL SELECT client_id, opportunity_id FROM crm_opportunities WHERE opportunity_id=? "
            "UNION ALL SELECT o.client_id, q.opportunity_id FROM crm_quotes q JOIN crm_opportunities o USING(opportunity_id) WHERE quote_id=? "
            "UNION ALL SELECT client_id, opportunity_id FROM crm_contracts WHERE contract_id=? "
            "UNION ALL SELECT o.client_id, a.opportunity_id FROM crm_attachments a JOIN crm_opportunities o USING(opportunity_id) WHERE attachment_id=?",
            (ref,) * 5,
        )
        if (
            len(matches) != 1
            or matches[0]["client_id"] != client["client_id"]
            or (entity_type == "opportunity" and matches[0]["opportunity_id"] != target)
        ):
            raise ValueError(
                "handoff reference is absent or not linked to the target entity"
            )
    for evidence_id in args["evidence_ids"]:
        record(world, "crm_evidence", "asset_id", evidence_id)
    row = {
        "note_id": world.next_id("crm_notes", "note_id", "NT-"),
        "entity_type": entity_type,
        "entity_id": target,
        "content": args["content"].strip(),
        "reference_ids_json": canonical_json(sorted(args["reference_ids"])),
        "evidence_ids_json": canonical_json(sorted(args["evidence_ids"])),
    }
    world.connection.execute(
        "INSERT INTO crm_notes VALUES (?,?,?,?,?,?)", tuple(row.values())
    )
    return changed(
        world, "arccrm.notes.create", "crm_notes", "note_id", row, args, "created"
    )


def tools() -> tuple[ToolSpec, ...]:
    result = []

    def add(
        name: str,
        description: str,
        properties: dict,
        handler: Any,
        *,
        write: bool = False,
        optional: tuple = (),
    ) -> None:
        names = SOURCE_NAMES.get(name)
        provenance = (
            f"Independently authored adaptation of observed {', '.join(names)}; not upstream ABI parity."
            if names
            else "Blobfish-only supporting evidence/readback extension; not an observed upstream contract."
        )
        result.append(
            ToolSpec(
                name,
                f"{description} {provenance}",
                obj(properties, [key for key in properties if key not in optional]),
                "write" if write else "read",
                handler,
                shape=provenance,
                idempotent=not write,
            )
        )

    for resource, columns, key in [
        ("clients", ("client_id", "name", "domain"), "client_id"),
        ("contacts", ("contact_id", "client_id", "email", "last_name"), "contact_id"),
        ("opportunities", ("opportunity_id", "client_id", "name"), "opportunity_id"),
        ("quotes", ("quote_id", "opportunity_id", "status"), "quote_id"),
        (
            "contracts",
            ("contract_id", "client_id", "opportunity_id", "quote_id"),
            "contract_id",
        ),
        ("documents", ("document_id", "client_id", "file_name"), "document_id"),
        (
            "attachments",
            ("attachment_id", "document_id", "opportunity_id"),
            "attachment_id",
        ),
        ("notes", ("note_id", "entity_id"), "note_id"),
    ]:
        add(
            f"arccrm.{resource}.search",
            "Literal case-insensitive search, all matches in deterministic ID order.",
            {"query": string()},
            lambda w, a, t=f"crm_{resource}", c=columns, k=key: search(w, a, t, c, k),
            optional=("query",),
        )
        add(
            f"arccrm.{resource}.get",
            "Read the persisted record by its immutable ID.",
            {key: ID},
            get_quote
            if resource == "quotes"
            else lambda w, a, t=f"crm_{resource}", k=key: record(w, t, k, a[k]),
        )
    add(
        "arccrm.contacts.create",
        "Create a contact on an authorized active existing client. Verified domain required; email is globally case-insensitive unique. Titles and account creation are not permitted.",
        {"client_id": ID, "first_name": TEXT, "last_name": TEXT, "email": TEXT},
        create_contact,
        write=True,
    )
    add(
        "arccrm.opportunities.update",
        "Update only stage/probability using the current revision and operative stage policy. All financial/owner fields remain unchanged.",
        {
            "opportunity_id": ID,
            "stage": string(
                enum=[
                    "discovery",
                    "qualified",
                    "proposal",
                    "negotiation",
                    "won",
                    "lost",
                ]
            ),
            "probability": integer(minimum=0, maximum=100),
            "expected_revision": integer(minimum=1),
        },
        update_opportunity,
        write=True,
    )
    add(
        "arccrm.quotes.create",
        "Create only a draft with current SKU prices, bounded approval and integer minor-unit total. Invoice-level discount rounds half up. One open draft/approved quote per opportunity; cancelled predecessor is optional for replacements.",
        {
            "opportunity_id": ID,
            "amount_minor": MONEY,
            "currency": string(enum=["USD", "EUR"]),
            "discount_bps": integer(minimum=0, maximum=10000),
            "approval_id": ID,
            "valid_until": string(pattern=r"^\d{4}-\d{2}-\d{2}$"),
            "status": string(enum=["draft", "approved", "signed"]),
            "predecessor_id": ID,
            "lines": {
                "type": "array",
                "items": obj(
                    {"sku": ID, "quantity": integer(minimum=1, maximum=10000)},
                    ["sku", "quantity"],
                ),
            },
        },
        create_quote,
        write=True,
        optional=("predecessor_id",),
    )
    add(
        "arccrm.quotes.cancel",
        "Cancel the authorized expected draft version. Signed/approved quotes and history cannot be deleted or edited.",
        {"quote_id": ID, "expected_version": integer(minimum=1)},
        cancel_quote,
        write=True,
    )
    add(
        "arccrm.quotes.compare",
        "Compare persisted same-currency quotes and the amount spread; comparison does not confer approval.",
        {"quote_ids": {"type": "array", "items": ID}},
        compare_quotes,
    )
    add(
        "arccrm.contracts.create",
        "Create an unsigned draft from a current approved quote. Exact client/opportunity/currency/value links and a role limit are enforced. Never signs or activates a contract.",
        {
            "client_id": ID,
            "opportunity_id": ID,
            "quote_id": ID,
            "value_minor": MONEY,
            "currency": string(enum=["USD", "EUR"]),
            "status": string(enum=["draft", "signed", "active"]),
        },
        create_contract,
        write=True,
    )
    add(
        "arccrm.documents.attach",
        "Associate an approved seeded mock:// document with the authorized opportunity of the same client. Exact filename required. No URL or filesystem fetch ever occurs.",
        {
            "document_url": TEXT,
            "entity_type": string(enum=["client", "opportunity", "quote"]),
            "entity_id": ID,
            "file_name": TEXT,
        },
        attach_document,
        write=True,
    )
    add(
        "arccrm.notes.create",
        "Persist a fact-grounded handoff on an authorized entity. References must exist on that entity; evidence IDs must exist. Literal facts and structured links are graded, not semantic prose quality.",
        {
            "entity_type": string(enum=["client", "opportunity"]),
            "entity_id": ID,
            "content": TEXT,
            "reference_ids": {"type": "array", "items": ID},
            "evidence_ids": {"type": "array", "items": ID},
        },
        create_note,
        write=True,
    )
    add(
        "desk.requests.search",
        "Search the full conversation thread, including superseded requests. Resolve sequence/status before changing state.",
        {"query": string()},
        lambda w, a: search(
            w,
            a,
            "crm_requests",
            ("thread_id", "request_id", "subject", "client_id"),
            "thread_id, sequence",
        ),
        optional=("query",),
    )
    add(
        "desk.requests.get",
        "Read one request; later current messages supersede earlier conflicting messages.",
        {"request_id": ID},
        lambda w, a: record(w, "crm_requests", "request_id", a["request_id"]),
    )
    add(
        "desk.policies.get",
        "Read a versioned policy and whether it is operative at the frozen task date.",
        {"policy_id": ID},
        get_policy,
    )
    add(
        "desk.permissions.list",
        "List the acting user's current and expired write grants. No grant means no permission. This does not select the requested business outcome.",
        {},
        lambda w, a: {
            "user_id": w.task["role"],
            "permissions": w.all(
                "SELECT * FROM crm_permissions WHERE user_id=? ORDER BY permission_id",
                (w.task["role"],),
            ),
        },
    )
    add(
        "desk.approvals.get",
        "Read immutable scoped pricing approval; check status, opportunity, currency and expiry.",
        {"approval_id": ID},
        lambda w, a: record(w, "crm_approvals", "approval_id", a["approval_id"]),
    )
    add(
        "desk.prices.list",
        "Return all price revisions for a currency, including expired versions. Use the latest currently effective revision per SKU.",
        {"currency": string(enum=["USD", "EUR"])},
        lambda w, a: {
            "prices": w.all(
                "SELECT * FROM crm_prices WHERE currency=? ORDER BY sku, revision",
                (a["currency"],),
            )
        },
    )
    add(
        "vault.files.get",
        "Read text extracted from a seeded synthetic asset with its asset metadata. Binary release assets have separate file-byte hashes; no arbitrary paths or URLs are accepted.",
        {"asset_id": ID},
        lambda w, a: w.one(
            "SELECT e.*, f.* FROM crm_evidence e JOIN evidence_files f USING(asset_id) WHERE asset_id=?",
            (a["asset_id"],),
        ),
    )
    return tuple(result)


TOOLS = tools()
