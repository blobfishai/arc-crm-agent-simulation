"""JSON-schema-subset validation for tool arguments and canonical encodings."""

from __future__ import annotations

import json
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def canonical_argument(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple(sorted((key, canonical_argument(item)) for key, item in value.items()))
    if isinstance(value, list):
        return tuple(canonical_argument(item) for item in value)
    return value


def _type_matches(value: Any, expected: str) -> bool:
    if expected == "any":
        return True
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def validate_schema(value: Any, schema: dict[str, Any], path: str = "arguments") -> None:
    """Raise ``ValueError`` when ``value`` does not satisfy the schema subset."""

    expected_type = schema.get("type")
    if isinstance(expected_type, list):
        if not any(_type_matches(value, candidate) for candidate in expected_type):
            raise ValueError(f"{path} must match one of {expected_type}")
    elif isinstance(expected_type, str) and not _type_matches(value, expected_type):
        raise ValueError(f"{path} must be {expected_type}")
    if "const" in schema and value != schema["const"]:
        raise ValueError(f"{path} must equal {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} must be one of {schema['enum']}")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        missing = [name for name in required if name not in value]
        if missing:
            raise ValueError(f"{path} missing required properties: {missing}")
        if schema.get("additionalProperties") is False:
            unexpected = sorted(set(value) - set(properties))
            if unexpected:
                raise ValueError(f"{path} has unexpected properties: {unexpected}")
        for name, item in value.items():
            if name in properties:
                validate_schema(item, properties[name], f"{path}.{name}")
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            validate_schema(item, schema["items"], f"{path}[{index}]")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise ValueError(f"{path} must be at least {schema['minLength']} characters")
        if "pattern" in schema:
            import re

            if not re.search(schema["pattern"], value):
                raise ValueError(f"{path} must match {schema['pattern']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ValueError(f"{path} must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise ValueError(f"{path} must be <= {schema['maximum']}")


def obj(properties: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties or {},
        "required": required or [],
        "additionalProperties": False,
    }


def string(description: str | None = None, **extra: Any) -> dict[str, Any]:
    value: dict[str, Any] = {"type": "string"}
    if description:
        value["description"] = description
    value.update(extra)
    return value


def integer(description: str | None = None, **extra: Any) -> dict[str, Any]:
    value: dict[str, Any] = {"type": "integer"}
    if description:
        value["description"] = description
    value.update(extra)
    return value


__all__ = [
    "canonical_argument",
    "canonical_json",
    "integer",
    "obj",
    "string",
    "validate_schema",
]
