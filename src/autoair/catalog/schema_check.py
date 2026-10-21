"""Validate a JSON value against a (param) JSON Schema.

Uses :mod:`jsonschema` when importable (Airflow depends on it) and a small built-in
validator (type / enum / const / anyOf / oneOf / items / prefixItems / properties /
required / additionalProperties) otherwise.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any


@lru_cache(maxsize=1)
def _jsonschema() -> Any:
    try:
        import jsonschema  # type: ignore[import-untyped,unused-ignore]
    except ImportError:  # pragma: no cover - depends on the environment
        return None
    return jsonschema


def json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (list, tuple)):
        return "array"
    return "object"


def _type_ok(expected: Any, value: Any) -> bool:
    allowed = [expected] if isinstance(expected, str) else list(expected)
    actual = json_type(value)
    return actual in allowed or (actual == "integer" and "number" in allowed)


def _minimal(schema: dict[str, Any], value: Any, path: str) -> list[str]:
    errors: list[str] = []
    where = f" at {path}" if path else ""
    for key in ("anyOf", "oneOf"):
        if key in schema:
            branches = [_minimal(s, value, path) for s in schema[key]]
            ok = sum(1 for b in branches if not b)
            if ok == 0 or (key == "oneOf" and ok > 1):
                return [f"{value!r} is not valid under any of the given schemas{where}"]
    if "type" in schema and not _type_ok(schema["type"], value):
        expected = schema["type"]
        exp = expected if isinstance(expected, str) else " or ".join(expected)
        return [f"expected {exp}, got {json_type(value)}{where}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{value!r} is not one of {schema['enum']!r}{where}")
    if "const" in schema and value != schema["const"]:
        errors.append(f"{value!r} was expected to be {schema['const']!r}{where}")
    if isinstance(value, (list, tuple)):
        prefix = schema.get("prefixItems") or []
        for i, item in enumerate(value):
            sub = prefix[i] if i < len(prefix) else schema.get("items")
            if isinstance(sub, dict):
                errors += _minimal(sub, item, f"{path}[{i}]")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"too many items{where}")
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"too few items{where}")
    if isinstance(value, dict):
        props: dict[str, Any] = schema.get("properties") or {}
        for req in schema.get("required") or []:
            if req not in value:
                errors.append(f"{req!r} is a required property{where}")
        extra = schema.get("additionalProperties", True)
        for k, v in value.items():
            if k in props:
                errors += _minimal(props[k], v, f"{path}.{k}" if path else str(k))
            elif extra is False:
                errors.append(f"additional property {k!r} is not allowed{where}")
            elif isinstance(extra, dict):
                errors += _minimal(extra, v, f"{path}.{k}" if path else str(k))
    return errors


def validate_value(schema: dict[str, Any], value: Any) -> list[str]:
    """Return human readable problems (empty when valid)."""
    if not schema:
        return []
    js = _jsonschema()
    if js is not None:
        try:
            cls = js.validators.validator_for(schema, default=js.Draft202012Validator)
            validator = cls(schema)
            errors = sorted(validator.iter_errors(value), key=lambda e: list(e.path))
        except Exception:  # broken schema: fall through to the lenient checker
            return _minimal(schema, value, "")
        out: list[str] = []
        for err in errors[:3]:
            loc = ".".join(str(p) for p in err.path)
            out.append(f"{err.message}" + (f" at {loc}" if loc else ""))
        return out
    return _minimal(schema, value, "")


__all__ = ["json_type", "validate_value"]
