"""The hub's published contracts (schemas/*.json) and a stdlib validator for the subset they use.

The JSON Schema files ARE the interface other services adopt: a producer in any language reads
`envelope.v1.json` to build its messages; nothing in this module is needed to publish one.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from telefi import messages

SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
SCHEMAS = ("envelope.v1", "inbound.v1", "hub_config.v1", "commands.v1")

_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool}


@lru_cache(maxsize=None)
def load(name: str) -> dict:
    return json.loads((SCHEMA_DIR / f"{name}.json").read_text(encoding="utf-8"))


def errors(obj: object, schema: dict, path: str = "$") -> list[str]:
    """Violations of type/required/enum/pattern/maxLength/maxItems/minItems/properties/additionalProperties/items."""
    out: list[str] = []
    if isinstance(obj, str) and "pattern" in schema and not re.search(schema["pattern"], obj):
        out.append(f"{path}: {obj[:60]!r} does not match {schema['pattern']}")
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
        if "minItems" in schema and len(obj) < schema["minItems"]:
            out.append(f"{path}: fewer than {schema['minItems']} items")
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


@lru_cache(maxsize=None)
def load_template(name: str) -> dict:
    return json.loads((TEMPLATE_DIR / f"{name}.json").read_text(encoding="utf-8"))


def template_errors(env: dict) -> list[str]:
    """An envelope's `template` must be known and its `facts` valid for it (nothing is half-rendered)."""
    name, facts = env.get("template"), env.get("facts")
    if name is None and facts is None:
        return []
    if name is None:
        return ["$: facts without a template"]
    if name not in messages.TEMPLATES:
        return [f"$.template: {name!r} is not one of {list(messages.TEMPLATES)}"]
    if not isinstance(facts, dict):
        return [f"$: template {name} needs facts"]
    bad = errors(facts, load_template(name), "$.facts") or messages.extra_errors(name, facts)
    if bad or not (isinstance(env.get("id"), str) and isinstance(env.get("source"), str)):
        return bad
    return messages.callback_errors(env)


def validate(obj: object, name: str) -> list[str]:
    found = errors(obj, load(name))
    if name == "envelope.v1" and isinstance(obj, dict):
        found += template_errors(obj)
    return found
