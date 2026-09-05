"""Isolated, stateful SQLite world for one HubBench task episode.

The same ``World`` backs the stdio MCP server, the terminal ``tool`` CLI, and
the in-process evaluator, so every surface sees identical state: writes
persist to real domain tables, readbacks reflect them, and the call trace is
durable in the database.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

from .families import CONTEXT_TOOL, ENGINE_TOOLS, SUBMIT_TOOL, Family, public_tool_definitions
from .validation import canonical_json, validate_schema

_CORE_SCHEMA_PATH = Path(__file__).with_name("core.sql")


def normalize_answer_fields(task: dict[str, Any], fields: dict[str, Any]) -> dict[str, str]:
    """Validate task-specific answer fields and return canonical text values."""

    schema = task["answer_schema"]
    properties = schema["properties"]
    expected_fields = set(properties)
    submitted_fields = set(fields)
    if submitted_fields != expected_fields:
        missing = sorted(expected_fields - submitted_fields)
        unexpected = sorted(submitted_fields - expected_fields)
        raise ValueError(f"answer fields do not match schema; missing={missing}, unexpected={unexpected}")
    normalized: dict[str, str] = {}
    for field, field_schema in properties.items():
        value = fields[field]
        answer_type = field_schema["type"]
        if answer_type == "string":
            if not isinstance(value, str):
                raise ValueError(f"answer field {field} must be a string")
            if "enum" in field_schema and value not in field_schema["enum"]:
                raise ValueError(f"answer field {field} must be one of {field_schema['enum']}")
            normalized[field] = value
            continue
        if isinstance(value, bool):
            raise ValueError(f"answer field {field} must be numeric")
        try:
            decimal_value = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"answer field {field} must be numeric") from exc
        if not decimal_value.is_finite():
            raise ValueError(f"answer field {field} must be finite")
        if answer_type == "integer":
            if decimal_value != decimal_value.to_integral_value():
                raise ValueError(f"answer field {field} must be an integer")
            normalized[field] = str(int(decimal_value))
            continue
        if answer_type == "number":
            quantum = Decimal(str(field_schema.get("multipleOf", 0.01)))
            quantized = decimal_value.quantize(quantum)
            if quantized != decimal_value:
                raise ValueError(f"answer field {field} exceeds the allowed precision")
            places = max(0, -quantum.as_tuple().exponent)
            normalized[field] = f"{quantized:.{places}f}"
            continue
        raise ValueError(f"unsupported answer type for {field}: {answer_type}")
    return normalized


def seed_database(family: Family, task: dict[str, Any], path: str | Path) -> Path:
    """Create a fresh deterministic SQLite world for one task."""

    database_path = Path(path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    if database_path.exists():
        database_path.unlink()
    connection = sqlite3.connect(database_path)
    try:
        connection.executescript(_CORE_SCHEMA_PATH.read_text(encoding="utf-8"))
        connection.executescript(family.schema_sql)
        connection.execute("PRAGMA foreign_keys = OFF")
        for table, rows in task["seed_tables"].items():
            for row in rows:
                columns = list(row)
                placeholders = ", ".join("?" for _ in columns)
                names = ", ".join(columns)
                connection.execute(
                    f"INSERT INTO {table} ({names}) VALUES ({placeholders})",
                    [json.dumps(row[column], sort_keys=True) if isinstance(row[column], (dict, list)) else row[column] for column in columns],
                )
        connection.commit()
        connection.execute("PRAGMA foreign_keys = ON")
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise ValueError(f"seed data violates foreign keys: {violations}")
    finally:
        connection.close()
    return database_path


class World:
    """An isolated task world with schema validation and transactional writes."""

    def __init__(self, family: Family, task: dict[str, Any], database_path: str | Path):
        self.family = family
        self.task = task
        self.database_path = Path(database_path)
        self.connection = sqlite3.connect(self.database_path)
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.row_factory = sqlite3.Row
        self.trace: list[dict[str, Any]] = self._load_trace()

    @classmethod
    def fresh(cls, family: Family, task: dict[str, Any], database_path: str | Path) -> "World":
        seed_database(family, task, database_path)
        return cls(family, task, database_path)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "World":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # Query helpers for family tool handlers
    # ------------------------------------------------------------------ #

    def one(self, query: str, params: Iterable[Any] = (), *, missing: str = "record not found") -> dict[str, Any]:
        row = self.connection.execute(query, tuple(params)).fetchone()
        if row is None:
            raise ValueError(missing)
        return dict(row)

    def all(self, query: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(query, tuple(params)).fetchall()]

    def next_id(self, table: str, column: str, prefix: str) -> str:
        """Deterministic next identifier: ``prefix`` + (max numeric suffix + 1)."""

        rows = self.all(f"SELECT {column} AS value FROM {table}")
        highest = 0
        for row in rows:
            value = str(row["value"])
            if value.startswith(prefix):
                suffix = value[len(prefix) :]
                if suffix.isdigit():
                    highest = max(highest, int(suffix))
        return f"{prefix}{highest + 1}"

    @property
    def as_of(self) -> date:
        return date.fromisoformat(self.task["as_of"])

    def clock(self) -> str:
        """Logical timestamp: as_of date plus one minute per recorded call."""

        base = datetime.combine(self.as_of, datetime.min.time()).replace(hour=9)
        return (base + timedelta(minutes=len(self.trace))).strftime("%Y-%m-%dT%H:%M:%S")

    def snapshot(self) -> dict[str, list[dict[str, Any]]]:
        tables = [
            row["name"]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        snapshot: dict[str, list[dict[str, Any]]] = {}
        for table in tables:
            if table == "call_trace":
                continue
            columns = [row["name"] for row in self.connection.execute(f"PRAGMA table_info({table})")]
            order = ", ".join(columns)
            rows = self.connection.execute(f"SELECT * FROM {table} ORDER BY {order}").fetchall()
            snapshot[table] = [dict(row) for row in rows]
        return snapshot

    # ------------------------------------------------------------------ #
    # Mutation and audit recording
    # ------------------------------------------------------------------ #

    def audit(self, tool: str, table_name: str, record_id: str, action: str, payload: dict[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO audit_log (task_id, tool, table_name, record_id, action, payload) VALUES (?, ?, ?, ?, ?, ?)",
            (self.task["task_id"], tool, table_name, record_id, action, canonical_json(payload)),
        )

    def record_mutation(self, tool: str, table_name: str, record_id: str, status: str, arguments: dict[str, Any], *, revision: int = 1) -> str:
        sequence = self.one("SELECT COUNT(*) AS n FROM mutations WHERE task_id = ?", (self.task["task_id"],))["n"] + 1
        mutation_id = f"{self.task['task_id']}-mutation-{sequence:02d}"
        payload = {"tool": tool, "arguments": arguments}
        self.connection.execute(
            "INSERT INTO mutations (mutation_id, task_id, sequence, tool, table_name, record_id, status, payload_json, effective_at, revision) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (mutation_id, self.task["task_id"], sequence, tool, table_name, record_id, status, canonical_json(payload), self.clock(), revision),
        )
        self.audit(tool, "mutations", mutation_id, "insert", payload)
        return mutation_id

    # ------------------------------------------------------------------ #
    # Tool dispatch
    # ------------------------------------------------------------------ #

    def tool_definitions(self) -> list[dict[str, Any]]:
        return public_tool_definitions(self.family, self.task["answer_schema"])

    def _input_schema(self, tool: str) -> dict[str, Any]:
        if tool == SUBMIT_TOOL:
            return self.task["answer_schema"]
        if tool == CONTEXT_TOOL:
            return {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
        return self.family.tool_by_name[tool].input_schema

    def _transient_fault(self, tool: str, arguments: dict[str, Any]) -> str | None:
        """Optional deterministic transient faults declared on the task."""

        for fault in self.task.get("transient_faults", []):
            if fault["tool"] != tool:
                continue
            match = fault.get("arguments_match", {})
            if any(arguments.get(key) != value for key, value in match.items()):
                continue
            prior = sum(
                1
                for entry in self.trace
                if entry["tool"] == tool and not entry["success"] and entry["result"].get("error") == fault["error"]
            )
            if prior < int(fault.get("failures", 1)):
                return str(fault["error"])
        return None

    def call_tool(self, tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        arguments = arguments or {}
        self._sync_trace()
        known = tool in self.family.tool_by_name or tool in ENGINE_TOOLS
        if not known:
            result: dict[str, Any] = {"error": f"unknown tool: {tool}"}
            self._record(tool, arguments, False, result)
            return result
        try:
            validate_schema(arguments, self._input_schema(tool))
            fault = self._transient_fault(tool, arguments)
            if fault is not None:
                raise TransientFault(fault)
            if tool == CONTEXT_TOOL:
                result = self._context()
            elif tool == SUBMIT_TOOL:
                result = self._submit_answer(arguments)
            else:
                result = self.family.tool_by_name[tool].handler(self, arguments)
            self.connection.commit()
            success = True
        except TransientFault as exc:
            self.connection.rollback()
            result = {"error": str(exc), "retryable": True}
            success = False
        except (KeyError, TypeError, ValueError, sqlite3.Error, json.JSONDecodeError) as exc:
            self.connection.rollback()
            result = {"error": str(exc)}
            success = False
        self._record(tool, arguments, success, result)
        return result

    def _record(self, tool: str, arguments: dict[str, Any], success: bool, result: dict[str, Any]) -> None:
        entry = {"index": len(self.trace), "tool": tool, "arguments": arguments, "success": success, "result": result}
        for attempt in range(2):
            try:
                self.connection.execute(
                    "INSERT INTO call_trace (trace_index, tool, arguments_json, success, result_json) VALUES (?, ?, ?, ?, ?)",
                    (entry["index"], tool, canonical_json(arguments), int(success), canonical_json(result)),
                )
                break
            except sqlite3.IntegrityError:
                # Another surface (CLI, stdio MCP, HTTP) appended to the durable
                # trace between our sync and this insert: take its entries first.
                if attempt:
                    raise
                self.trace = self._load_trace()
                entry["index"] = len(self.trace)
        self.trace.append(entry)
        self.connection.commit()

    def _sync_trace(self) -> None:
        """Reload the durable trace when another surface on the same database extended it."""

        try:
            count = self.connection.execute("SELECT COUNT(*) AS n FROM call_trace").fetchone()["n"]
        except sqlite3.OperationalError:
            return
        if count != len(self.trace):
            self.trace = self._load_trace()

    def _load_trace(self) -> list[dict[str, Any]]:
        try:
            rows = self.connection.execute("SELECT trace_index, tool, arguments_json, success, result_json FROM call_trace ORDER BY trace_index").fetchall()
        except sqlite3.OperationalError:
            return []
        return [
            {
                "index": row["trace_index"],
                "tool": row["tool"],
                "arguments": json.loads(row["arguments_json"]),
                "success": bool(row["success"]),
                "result": json.loads(row["result_json"]),
            }
            for row in rows
        ]

    # ------------------------------------------------------------------ #
    # Engine tools
    # ------------------------------------------------------------------ #

    def _context(self) -> dict[str, Any]:
        evidence = self.all(
            "SELECT asset_id, path, title, kind, source, media_type, sha256 FROM evidence_files WHERE task_id = ? ORDER BY path",
            (self.task["task_id"],),
        )
        contracts = self.family.server_contracts()
        mounted = [
            {"name": server, "description": contracts[server]["description"], "tools": contracts[server]["tools"]}
            for server in [*self.task["world"]["systems"], "hubbench"]
            if server in contracts
        ]
        return {
            "task": {
                "task_id": self.task["task_id"],
                "family": self.task["family"],
                "role": self.task["role"],
                "as_of": self.task["as_of"],
            },
            "organization": self.task["world"],
            "state": {"scope": "isolated task snapshot", "persistence": "episode-local SQLite", "network": "closed"},
            "identity": self.all("SELECT user_id, display_name, role, approval_limit_usd FROM users ORDER BY user_id"),
            "starting_records": self.task.get("starting_records", []),
            "reference_records": self.task.get("reference_records", {}),
            "evidence_index": evidence,
            "tool_servers": mounted,
        }

    def _submit_answer(self, fields: dict[str, Any]) -> dict[str, Any]:
        normalized = normalize_answer_fields(self.task, fields)
        for field, value in normalized.items():
            self.connection.execute(
                "INSERT INTO answers (task_id, field, value) VALUES (?, ?, ?) ON CONFLICT(task_id, field) DO UPDATE SET value = excluded.value",
                (self.task["task_id"], field, value),
            )
        self.audit(SUBMIT_TOOL, "answers", self.task["task_id"], "submit", normalized)
        return {"accepted": True, "task_id": self.task["task_id"], "fields": normalized}


class TransientFault(RuntimeError):
    """A declared, retryable provider fault."""


__all__ = ["TransientFault", "World", "normalize_answer_fields", "seed_database"]
