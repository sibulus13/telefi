"""The hub's published contracts (schemas/*.json) and a stdlib validator for the subset they use.

The JSON Schema files ARE the interface other services adopt: a producer in any language reads
`envelope.v1.json` to build its messages; nothing in this module is needed to publish one.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"
SCHEMAS = ("envelope.v1", "inbound.v1", "hub_config.v1", "commands.v1")

_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool}


@lru_cache(maxsize=None)
def load(name: str) -> dict:
    return json.loads((SCHEMA_DIR / f"{name}.json").read_text(encoding="utf-8"))


def errors(obj: object, schema: dict, path: str = "$") -> list[str]:
    """Violations of type/required/enum/maxLength/maxItems/properties/additionalProperties/items."""
    out: list[str] = []
    t = schema.get("type")
    if t == "number":
        if not isinstance(obj, (int, float)) or isinstance(obj, bool):
            return [f"{path}: expected number"]
    elif t and not isinstance(obj, _TYPES[t]):
        return [f"{path}: expected {t}"]
    if "enum" in schema and obj not in schema["enum"]:
        out.append(f"{path}: {obj!r} not in {schema['enum']}")
    if isinstance(obj, str) and "maxLength" in schema and len(obj) > schema["maxLength"]:
        out.append(f"{path}: longer than {schema['maxLength']}")
    if isinstance(obj, list):
        if "maxItems" in schema and len(obj) > schema["maxItems"]:
            out.append(f"{path}: more than {schema['maxItems']} items")
        for i, item in enumerate(obj):
            out += errors(item, schema.get("items", {}), f"{path}[{i}]")
    if isinstance(obj, dict):
        for key in schema.get("required", []):
            if key not in obj:
                out.append(f"{path}: missing {key}")
        props = schema.get("properties", {})
        extra = schema.get("additionalProperties", True)
        for key, val in obj.items():
            if key in props:
                out += errors(val, props[key], f"{path}.{key}")
            elif extra is False:
                out.append(f"{path}: unexpected field {key}")
            elif isinstance(extra, dict):
                out += errors(val, extra, f"{path}.{key}")
    return out


def validate(obj: object, name: str) -> list[str]:
    return errors(obj, load(name))
