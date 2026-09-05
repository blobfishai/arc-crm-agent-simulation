"""Blobfish Arc CRM: a separate, partial clean-room source adaptation."""

import json
from pathlib import Path

from benchmark.hubbench.engine.families import Family

from .tools import TOOLS

ROOT = Path(__file__).parent
SOURCE_LOCK = json.loads((ROOT / "source.json").read_text())


def build_tasks():
    from .tasks import build_tasks as build

    return build()


FAMILY = Family(
    slug="arc-crm",
    name="Blobfish Arc CRM",
    version=SOURCE_LOCK["adapter_version"],
    cluster="crm",
    description="Six independently authored CRM workflows inspired by a pinned Arc CRM source; partial coverage, not upstream API parity.",
    schema_sql=(ROOT / "schema.sql").read_text(),
    servers={
        "arccrm": "Stateful synthetic CRM",
        "desk": "Mock requests, permissions, approvals and versioned policy/price records",
        "vault": "Seeded synthetic evidence; no external file access",
    },
    tools=TOOLS,
    build_tasks=build_tasks,
    as_of="2026-09-05",
)

__all__ = ["FAMILY", "SOURCE_LOCK", "build_tasks"]
