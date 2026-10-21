"""Describe every setting (type, default, description, secret flag) and dump effective values.

Shared by ``GET /api/config`` (:mod:`autoair.api.routers.config`) and
``scripts/gen_config_docs.py`` (``docs/configuration-reference.md``). Nested sections (any
field whose type is a pydantic model) are walked recursively; ``dict[str, Model]`` fields
(``team_storage``) are single options whose values are masked recursively.

A field is **secret** when its type is ``SecretStr``/``SecretBytes``, when it is marked with
``json_schema_extra={"secret": True}`` or when its name ends like a credential (``token``,
``password``, ``api_key``, ``private_key``, ``secret``, ``credentials``, ``headers``) and it
is not marked ``json_schema_extra={"secret": False}`` (a mode switch such as
``storage.git.request_token``).
"""

from __future__ import annotations

import re
import types
import typing
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel, SecretBytes, SecretStr
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined, to_jsonable_python

ENV_PREFIX = "AUTOAIR_"
MASK = "***"
_SECRET_NAME = re.compile(
    r"(^|_)(token|password|passwd|secret|api_key|private_key|credentials?|headers)$"
)

# Keys of free-form option maps (fsspec storage_options, headers…) whose values are
# credentials: masked in /api/config and the deploy-target API, never persisted.
_CREDENTIAL_KEY = re.compile(
    r"(^|[_\-.])(key|secret|token|password|passwd|pwd|credentials?|sas|sas_token|"
    r"account_key|connection_string|client_secret|private_key|authorization|auth|"
    r"session_token|security_token|cookie|signature)$"
    r"|access_key|secret|password|credential|token|connection_string|private_key",
    re.I,
)
# scheme://userinfo@host — userinfo is "user:password" or a bare token used as the user.
_URL_USERINFO = re.compile(r"(?P<scheme>\b[A-Za-z][A-Za-z0-9+.\-]*://)(?P<info>[^/@\s?#]+)@")
_PUBLIC_USERS = frozenset({"git", "ssh", "anonymous"})


def is_credential_key(name: object) -> bool:
    """A key of a free-form options map that holds a credential (``key``, ``secret``,
    ``aws_secret_access_key``, ``account_key``, ``sas_token``, ``connection_string``…)."""
    return isinstance(name, str) and bool(_CREDENTIAL_KEY.search(name))


def mask_url_credentials(text: str) -> str:
    """``https://user:pass@host`` → ``https://user:***@host``; a bare token as user
    (``https://ghp_x@host``) → ``https://***@host``. Other text is returned unchanged."""

    def repl(m: re.Match[str]) -> str:
        user, sep, _password = m["info"].partition(":")
        if sep:
            return f"{m['scheme']}{user}:{MASK}@"
        return m[0] if user.lower() in _PUBLIC_USERS else f"{m['scheme']}{MASK}@"

    return _URL_USERINFO.sub(repl, text) if "@" in text else text


def mask_credentials(value: Any) -> Any:
    """Plain data (dicts, lists, strings) with credential-like keys masked and URL userinfo
    removed from every string, recursively."""
    if isinstance(value, dict):
        return {
            k: (MASK if v not in (None, "", {}, []) else v)
            if is_credential_key(k)
            else mask_credentials(v)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [mask_credentials(v) for v in value]
    if isinstance(value, str):
        return mask_url_credentials(value)
    return value


@dataclass(frozen=True)
class OptionDoc:
    key: str  # dotted path, also the configuration-file key
    section: str  # dotted path of the section ("" = top level)
    name: str
    env: str
    type: str
    default: Any  # JSON-able
    default_text: str  # human readable (docs)
    description: str
    secret: bool
    field: FieldInfo


def _extra(field: FieldInfo) -> dict[str, Any]:
    extra = field.json_schema_extra
    return extra if isinstance(extra, dict) else {}


def _strip_optional(annotation: Any) -> Any:
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    if origin is typing.Annotated:
        return _strip_optional(get_args(annotation)[0])
    return annotation


def section_model(annotation: Any) -> type[BaseModel] | None:
    """The model of a nested section field (``StorageSettings``…), else ``None``."""
    inner = _strip_optional(annotation)
    if isinstance(inner, type) and issubclass(inner, BaseModel):
        return inner
    return None


def type_name(annotation: Any) -> str:
    """Readable type: ``int``, ``list[str]``, ``'pr' | 'push'``, ``str | None``…"""
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is typing.Annotated:
        return type_name(args[0])
    if origin is Literal:
        return " | ".join(repr(a) for a in args)
    if origin in (Union, types.UnionType):
        return " | ".join(type_name(a) for a in args)
    if origin is not None:
        name = getattr(origin, "__name__", str(origin))
        return f"{name}[{', '.join(type_name(a) for a in args)}]" if args else name
    if annotation is type(None):
        return "None"
    if annotation in (SecretStr, SecretBytes):
        return "secret str"
    if annotation is Path:
        return "path"
    if isinstance(annotation, type):
        return annotation.__name__
    return str(annotation).replace("typing.", "")


def _contains(annotation: Any, kinds: tuple[type, ...]) -> bool:
    if annotation in kinds:
        return True
    return any(_contains(a, kinds) for a in get_args(annotation))


def is_secret(name: str, field: FieldInfo) -> bool:
    secret = _extra(field).get("secret")
    if secret is True:
        return True
    if secret is False:  # a name that only looks like a credential (git.request_token)
        return False
    if _contains(field.annotation, (SecretStr, SecretBytes)):
        return True
    return bool(_SECRET_NAME.search(name))


def _default(field: FieldInfo) -> tuple[Any, str]:
    extra = _extra(field)
    if "doc_default" in extra:
        return None, str(extra["doc_default"])
    if field.default is not PydanticUndefined:
        value = field.default
    elif field.default_factory is not None:
        try:
            value = field.default_factory()  # type: ignore[call-arg]
        except Exception:
            return None, "(computed)"
    else:
        return None, "(required)"
    if isinstance(value, BaseModel):
        return None, "(section)"
    if isinstance(value, Enum):
        value = value.value
    json_value = to_jsonable_python(value, fallback=str)
    if json_value is None:
        return None, "None"
    if isinstance(json_value, str):
        return json_value, json_value
    import json

    return json_value, json.dumps(json_value, ensure_ascii=False)


def iter_options(model: type[BaseModel], prefix: tuple[str, ...] = ()) -> Iterator[OptionDoc]:
    """Every leaf option of ``model``, sections recursed (declaration order)."""
    for name, field in model.model_fields.items():
        path = (*prefix, name)
        nested = section_model(field.annotation)
        if nested is not None:
            yield from iter_options(nested, path)
            continue
        default, default_text = _default(field)
        yield OptionDoc(
            key=".".join(path),
            section=".".join(prefix),
            name=name,
            env=ENV_PREFIX + "__".join(path).upper(),
            type=type_name(field.annotation),
            default=default,
            default_text=default_text,
            description=" ".join((field.description or "").split()),
            secret=is_secret(name, field),
            field=field,
        )


def iter_sections(
    model: type[BaseModel], prefix: tuple[str, ...] = ()
) -> Iterator[tuple[str, type[BaseModel], str]]:
    """``(dotted path, model, description)`` of ``model`` and every nested section."""
    yield ".".join(prefix), model, ""
    for name, field in model.model_fields.items():
        nested = section_model(field.annotation)
        if nested is not None:
            path = (*prefix, name)
            for sub, sub_model, desc in iter_sections(nested, path):
                yield (
                    sub,
                    sub_model,
                    desc
                    or (
                        " ".join((field.description or "").split()) if sub == ".".join(path) else ""
                    ),
                )


# --------------------------------------------------------------------------- values


def masked(value: Any) -> Any:
    """JSON-able ``value`` with every secret field of nested models replaced by ``***``."""
    if isinstance(value, BaseModel):
        out: dict[str, Any] = {}
        for name, field in type(value).model_fields.items():
            item = getattr(value, name, None)
            if is_secret(name, field):
                out[name] = MASK if item not in (None, "", {}, []) else to_jsonable_python(item)
            else:
                out[name] = masked(item)
        return out
    if isinstance(value, dict):
        return {
            str(k): (MASK if v not in (None, "", {}, []) else masked(v))
            if is_credential_key(k)
            else masked(v)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [masked(v) for v in value]
    if isinstance(value, (SecretStr, SecretBytes)):
        return MASK
    # URLs / DSNs with user:password@ (database URLs, git remotes, fsspec URLs…).
    return mask_credentials(to_jsonable_python(value, fallback=str))


def value_at(settings: BaseModel, key: str) -> Any:
    node: Any = settings
    for part in key.split("."):
        node = getattr(node, part, None)
    return node


__all__ = [
    "MASK",
    "OptionDoc",
    "is_credential_key",
    "is_secret",
    "iter_options",
    "iter_sections",
    "mask_credentials",
    "mask_url_credentials",
    "masked",
    "section_model",
    "type_name",
    "value_at",
]
