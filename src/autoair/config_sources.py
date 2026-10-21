"""Configuration sources beyond environment variables: Python, JSON, YAML and INI files.

Precedence (highest first)::

    explicit init arguments  (Settings(subfolder="x"), CLI options such as --dags-folder)
    environment variables    (AUTOAIR_*)
    .env file                (dotenv)
    secrets directory        (pydantic-settings secrets_dir, when configured)
    configuration files      (later files override earlier ones)
    defaults

Configuration files are named by (in this order, all of them are loaded):

1. ``[autoair] config_file`` in ``airflow.cfg`` (or ``AIRFLOW__AUTOAIR__CONFIG_FILE``);
2. ``AUTOAIR_CONFIG_FILE`` — one reference, a comma separated list or a JSON list;
3. ``autoair serve --config …`` (appended to ``AUTOAIR_CONFIG_FILE``) and
   :func:`load_settings` ``config_files=``.

A *reference* is one of:

* ``/etc/autoair/config.yaml`` / ``.yml`` — YAML (PyYAML, ``pip install 'autoair[yaml]'``);
* ``/etc/autoair/config.json`` — JSON;
* ``/etc/autoair/autoair.ini`` / ``.cfg`` / ``.conf`` — INI, Airflow style (see below);
* ``/etc/autoair/config.py`` or ``/etc/autoair/config.py:attr`` — a Python file;
* ``acme.autoair_config`` or ``acme.autoair_config:attr`` — an importable Python module.

A Python attribute is a ``dict``, a :class:`~autoair.settings.Settings` (only the values it
was given are used) or a callable returning one of those. Without ``:attr`` the attributes
``AUTOAIR_CONFIG``, ``config`` and ``settings`` are tried, in that order.

INI mapping: the ``[autoair]`` section holds top-level settings; ``[autoair.<a>.<b>]`` sections
hold nested ones (``[autoair.storage.git]`` → ``storage.git.*``); a key may also be dotted
(``git.mode = push`` in ``[autoair.storage]``). Values that start with ``[``, ``{`` or ``"`` are
parsed as JSON (lists, maps, quoted strings); everything else is a string that pydantic converts
(``true``/``false``, numbers…). Other sections are ignored, so ``airflow.cfg`` itself can be used.
"""

from __future__ import annotations

import configparser
import contextvars
import copy
import hashlib
import importlib
import importlib.util
import json
import logging
import os
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any, ClassVar

from pydantic import BaseModel, PrivateAttr, SecretBytes, SecretStr, ValidationError
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

log = logging.getLogger(__name__)

CONFIG_FILE_ENV = "AUTOAIR_CONFIG_FILE"
AIRFLOW_CONFIG_FILE_ENV = "AIRFLOW__AUTOAIR__CONFIG_FILE"
INI_SECTION = "autoair"
PYTHON_ATTRS = ("AUTOAIR_CONFIG", "config", "settings")
INI_SUFFIXES = frozenset({".ini", ".cfg", ".conf"})
YAML_SUFFIXES = frozenset({".yaml", ".yml"})

# Files given programmatically (load_settings(config_files=...)), after the environment's.
_EXPLICIT_FILES: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar(
    "autoair_config_files", default=()
)
# Where each value came from, collected while a settings object is built.
_ORIGINS: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "autoair_config_origins", default=None
)
# Set while a Python config file is executed: a Settings() built inside it must not read
# configuration files again (endless recursion).
_LOADING: contextvars.ContextVar[bool] = contextvars.ContextVar("autoair_loading", default=False)


class ConfigError(ValueError):
    """Unreadable configuration file or invalid configuration value."""


# --------------------------------------------------------------------------- file references


def parse_file_list(value: str | Sequence[str] | None) -> list[str]:
    """``"a.yaml"`` / ``"a.yaml, b.ini"`` / ``'["a.yaml", "b.ini"]'`` / a list -> references."""
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                items = json.loads(text)
            except ValueError as exc:
                raise ConfigError(f"{CONFIG_FILE_ENV}: invalid JSON list: {exc}") from exc
            return [str(i).strip() for i in items if str(i).strip()]
        return [part.strip() for part in text.split(",") if part.strip()]
    return [str(v).strip() for v in value if str(v).strip()]


def _airflow_cfg_files() -> list[str]:
    """``[autoair] config_file`` of Airflow's configuration (read without importing Airflow)."""
    env = os.environ.get(AIRFLOW_CONFIG_FILE_ENV)
    if env:
        return parse_file_list(env)
    conf_mod = sys.modules.get("airflow.configuration")
    conf = getattr(conf_mod, "conf", None)
    if conf is not None:
        try:
            return parse_file_list(conf.get(INI_SECTION, "config_file", fallback=None))
        except Exception:
            log.debug("cannot read [autoair] config_file from Airflow", exc_info=True)
            return []
    home = Path(os.environ.get("AIRFLOW_HOME", "~/airflow")).expanduser()
    cfg_path = Path(os.environ.get("AIRFLOW_CONFIG", str(home / "airflow.cfg"))).expanduser()
    if not cfg_path.is_file():
        return []
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(cfg_path, encoding="utf-8")
    except configparser.Error:
        return []
    value = parser.get(INI_SECTION, "config_file", fallback=None)
    return parse_file_list(value.replace("{AIRFLOW_HOME}", str(home)) if value else None)


def configured_files() -> list[str]:
    """Every configuration file reference in effect, lowest precedence first."""
    files = [*_airflow_cfg_files(), *parse_file_list(os.environ.get(CONFIG_FILE_ENV))]
    files += list(_EXPLICIT_FILES.get())
    return list(dict.fromkeys(files))


def add_config_files(refs: Sequence[str]) -> None:
    """Append references to ``AUTOAIR_CONFIG_FILE`` (inherited by subprocesses)."""
    current = parse_file_list(os.environ.get(CONFIG_FILE_ENV))
    merged = list(dict.fromkeys([*current, *(str(r) for r in refs)]))
    if merged:
        os.environ[CONFIG_FILE_ENV] = json.dumps(merged)


# --------------------------------------------------------------------------- loaders


def _split_ref(ref: str) -> tuple[str, str | None]:
    """``path.py:attr`` / ``pkg.mod:attr`` -> ``(target, attr)``; Windows drives stay intact."""
    if Path(ref).expanduser().exists():
        return ref, None
    head, sep, attr = ref.rpartition(":")
    if sep and head and attr.isidentifier() and len(head) > 1:
        return head, attr
    return ref, None


def _python_object(obj: Any, ref: str) -> dict[str, Any]:
    if callable(obj) and not isinstance(obj, (BaseModel, Mapping, type)):
        obj = obj()
    if isinstance(obj, BaseModel):
        dumped: dict[str, Any] = _unwrap(obj.model_dump(exclude_unset=True))
        return dumped
    if isinstance(obj, Mapping):
        # The module is cached: never hand out its (mutable) nested values.
        try:
            return copy.deepcopy(dict(obj))
        except Exception:
            return dict(obj)
    raise ConfigError(
        f"{ref}: expected a dict, a Settings object or a callable returning one, "
        f"got {type(obj).__name__}"
    )


def _unwrap(value: Any) -> Any:
    if isinstance(value, (SecretStr, SecretBytes)):
        return value.get_secret_value()
    if isinstance(value, dict):
        return {k: _unwrap(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_unwrap(v) for v in value]
    return value


# Executed Python configuration files by (resolved path, mtime, size): a Settings() built per
# request must not run the file again (it is re-executed when the file changes).
_PY_CACHE: dict[Path, tuple[tuple[int, int], ModuleType]] = {}


def _exec_python_file(path: Path, ref: str) -> ModuleType:
    resolved = path.resolve()
    st = resolved.stat()
    stamp = (st.st_mtime_ns, st.st_size)
    cached = _PY_CACHE.get(resolved)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    name = f"_autoair_config_{hashlib.sha256(str(resolved).encode()).hexdigest()[:16]}"
    spec = importlib.util.spec_from_file_location(name, resolved)
    if spec is None or spec.loader is None:
        raise ConfigError(f"{ref}: cannot load Python file")
    module = importlib.util.module_from_spec(spec)
    # Registered while it runs: @dataclass (with postponed annotations), pickle, typing
    # helpers… look the module up in sys.modules.
    previous = sys.modules.get(name)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous
    _PY_CACHE[resolved] = (stamp, module)
    return module


def clear_python_cache() -> None:
    """Forget executed Python configuration files (tests)."""
    _PY_CACHE.clear()


def _load_python(target: str, attr: str | None, ref: str) -> dict[str, Any]:
    path = Path(target).expanduser()
    token = _LOADING.set(True)
    try:
        if path.suffix == ".py" or path.is_file():
            if not path.is_file():
                raise ConfigError(f"{ref}: file not found")
            module = _exec_python_file(path, ref)
        else:
            module = importlib.import_module(target)
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError(f"{ref}: {type(exc).__name__}: {exc}") from exc
    finally:
        _LOADING.reset(token)
    names = [attr] if attr else list(PYTHON_ATTRS)
    for name in names:
        if hasattr(module, name):
            token = _LOADING.set(True)
            try:
                return _python_object(getattr(module, name), ref)
            except ConfigError:
                raise
            except Exception as exc:
                raise ConfigError(f"{ref}: {type(exc).__name__}: {exc}") from exc
            finally:
                _LOADING.reset(token)
    raise ConfigError(f"{ref}: no attribute {' / '.join(names)}")


def _ini_value(raw: str) -> Any:
    text = raw.strip()
    if text[:1] in ("[", "{", '"'):
        try:
            return json.loads(text)
        except ValueError:
            return text
    return text


def _set_path(data: dict[str, Any], path: Sequence[str], value: Any, ref: str) -> None:
    node = data
    for part in path[:-1]:
        child = node.setdefault(part, {})
        if not isinstance(child, dict):
            raise ConfigError(f"{ref}: {'.'.join(path)} conflicts with the value of {part!r}")
        node = child
    node[path[-1]] = value


def _load_ini(path: Path, ref: str) -> dict[str, Any]:
    parser = configparser.ConfigParser(interpolation=None, default_section="\0none")
    parser.optionxform = str  # type: ignore[assignment,method-assign]  # keep key case
    try:
        with path.open(encoding="utf-8") as fh:
            parser.read_file(fh)
    except configparser.Error as exc:
        raise ConfigError(f"{ref}: {exc}") from exc
    data: dict[str, Any] = {}
    for section in parser.sections():
        if section == INI_SECTION:
            prefix: list[str] = []
        elif section.startswith(f"{INI_SECTION}."):
            prefix = [p for p in section[len(INI_SECTION) + 1 :].split(".") if p]
        else:
            continue
        for key, raw in parser.items(section, raw=True):
            if not prefix and key == "config_file":  # airflow.cfg pointer, not a setting
                continue
            parts = [p for p in key.replace("__", ".").split(".") if p]
            _set_path(data, [*prefix, *parts], _ini_value(raw), ref)
    return data


def _load_yaml(text: str, ref: str) -> Any:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - PyYAML is installed with autoair
        raise ConfigError(
            f"{ref}: YAML configuration files need PyYAML: pip install 'autoair[yaml]'"
        ) from exc
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{ref}: invalid YAML: {exc}") from exc


def _module_exists(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def load_config_file(ref: str) -> dict[str, Any]:
    """Read one configuration reference into a (nested) dict."""
    target, attr = _split_ref(ref)
    path = Path(target).expanduser()
    suffix = path.suffix.lower()
    known_suffix = suffix in (".py", ".json", *INI_SUFFIXES, *YAML_SUFFIXES)
    if suffix == ".py":
        return _load_python(target, attr, ref)
    if not path.exists():
        if all(p.isidentifier() for p in target.split(".")):
            if not known_suffix:
                return _load_python(target, attr, ref)  # importable module (pkg.module[:attr])
            # ``acme.cfg`` / ``acme.json``: a module whose last part looks like a suffix.
            if _module_exists(target):
                return _load_python(target, attr, ref)
        raise ConfigError(f"{ref}: file not found")
    if suffix in INI_SUFFIXES:
        return _load_ini(path, ref)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"{ref}: {exc}") from exc
    if suffix in YAML_SUFFIXES:
        data = _load_yaml(text, ref)
    elif suffix == ".json":
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise ConfigError(f"{ref}: invalid JSON: {exc}") from exc
    else:
        raise ConfigError(
            f"{ref}: unknown configuration format {suffix or '(no suffix)'!r} "
            "(use .py, .json, .yaml/.yml or .ini/.cfg/.conf)"
        )
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{ref}: the top level must be a mapping, got {type(data).__name__}")
    # A file may wrap everything in an "autoair" key (like the INI section).
    if set(data) == {INI_SECTION} and isinstance(data[INI_SECTION], dict):
        data = data[INI_SECTION]
    result: dict[str, Any] = data
    return result


def deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def leaf_paths(data: Mapping[str, Any], prefix: str = "") -> Iterator[str]:
    for key, value in data.items():
        path = f"{prefix}{key}"
        if isinstance(value, Mapping) and value:
            yield from leaf_paths(value, f"{path}.")
        else:
            yield path


# --------------------------------------------------------------------------- sources


def _record(data: Mapping[str, Any], origin: str | Callable[[str], str]) -> None:
    origins = _ORIGINS.get()
    if origins is None:
        return
    for path in leaf_paths(data):
        # Sources run highest precedence first: the first one to provide a value wins.
        origins.setdefault(path, origin(path) if callable(origin) else origin)


class TrackedSource(PydanticBaseSettingsSource):
    """Wraps a built-in source and records which values it provided (``/api/config``)."""

    def __init__(self, inner: PydanticBaseSettingsSource, origin: str) -> None:
        super().__init__(inner.settings_cls)
        self.inner = inner
        self.origin = origin

    def _set_current_state(self, state: dict[str, Any]) -> None:
        super()._set_current_state(state)
        self.inner._set_current_state(state)

    def _set_settings_sources_data(self, states: dict[str, dict[str, Any]]) -> None:
        super()._set_settings_sources_data(states)
        self.inner._set_settings_sources_data(states)

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return self.inner.get_field_value(field, field_name)

    def __call__(self) -> dict[str, Any]:
        data = self.inner()
        _record(data, self.origin)
        return data

    def __repr__(self) -> str:
        return f"TrackedSource({self.inner!r}, origin={self.origin!r})"


class ConfigFileSource(PydanticBaseSettingsSource):
    """Every configured file, merged in order (later files override earlier ones)."""

    def __init__(
        self, settings_cls: type[BaseSettings], files: Sequence[str] | None = None
    ) -> None:
        super().__init__(settings_cls)
        self._files = list(files) if files is not None else None
        self.loaded: list[str] = []

    @property
    def files(self) -> list[str]:
        if self._files is not None:
            return self._files
        return [] if _LOADING.get() else configured_files()

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False  # __call__ does the work

    def __call__(self) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        origin: dict[str, str] = {}
        strict = getattr(self.settings_cls, "warn_unknown_file_keys", False)
        known = _known_keys(self.settings_cls)
        for ref in self.files:
            data = load_config_file(ref)
            if strict:
                for key in data:
                    if key not in known:
                        log.warning("%s: unknown setting %r is ignored", ref, key)
            for path in leaf_paths(data):
                origin[path] = f"file:{ref}"
            merged = deep_merge(merged, data)
            self.loaded.append(ref)
        # Unknown keys are passed on: the model ignores them (extra="ignore").
        _record(merged, lambda path: origin.get(path, "file"))
        return merged

    def __repr__(self) -> str:
        return f"ConfigFileSource(files={self.files!r})"


def _known_keys(settings_cls: type[BaseModel]) -> set[str]:
    keys: set[str] = set()
    for name, field in settings_cls.model_fields.items():
        keys.add(name)
        if isinstance(field.alias, str):
            keys.add(field.alias)
    return keys


class AutoAirBaseSettings(BaseSettings):
    """``BaseSettings`` reading configuration files too and remembering each value's source."""

    # ``AUTOAIR_AI__PROVIDER=`` (an empty variable or .env value) means "unset": the default
    # applies instead of failing validation (enums) or becoming "." (paths). Subclasses'
    # model_config is merged with this one.
    model_config = SettingsConfigDict(env_ignore_empty=True)

    # Log unknown top-level keys of configuration files (the main Settings only: partial
    # settings classes such as the catalog's read the same files).
    warn_unknown_file_keys: ClassVar[bool] = False
    _origins: dict[str, str] = PrivateAttr(default_factory=dict)

    def __init__(self, **values: Any) -> None:
        token = _ORIGINS.set({})
        try:
            super().__init__(**values)
        except ValidationError as exc:
            raise ConfigError(format_validation_error(exc, _ORIGINS.get() or {})) from exc
        else:
            self._origins = dict(_ORIGINS.get() or {})
        finally:
            _ORIGINS.reset(token)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            TrackedSource(init_settings, "init"),
            TrackedSource(env_settings, "env"),
            TrackedSource(dotenv_settings, "dotenv"),
            TrackedSource(file_secret_settings, "secrets"),
            ConfigFileSource(settings_cls),
        )

    def origin_of(self, path: str) -> str:
        """``init`` · ``env`` · ``dotenv`` · ``secrets`` · ``file:<ref>`` · ``default``."""
        origins = self._origins
        if path in origins:
            return origins[path]
        prefix = f"{path}."
        for key, origin in origins.items():  # a value given below a dict/section field
            if key.startswith(prefix):
                return origin
        parts = path.split(".")
        for i in range(len(parts) - 1, 0, -1):  # a whole section given at once
            parent = ".".join(parts[:i])
            if parent in origins:
                return origins[parent]
        return "default"

    @property
    def origins(self) -> dict[str, str]:
        return dict(self._origins)


def format_validation_error(exc: ValidationError, origins: Mapping[str, str]) -> str:
    lines = [f"invalid AutoAir configuration ({exc.error_count()} error(s)):"]
    for err in exc.errors():
        key = ".".join(str(p) for p in err["loc"])
        origin = origins.get(key)
        if origin is None:
            parts = key.split(".")
            for i in range(len(parts) - 1, 0, -1):
                origin = origins.get(".".join(parts[:i]))
                if origin:
                    break
        where = {
            None: "",
            "init": " (from an argument)",
            "env": f" (from environment variable AUTOAIR_{key.replace('.', '__').upper()})",
            "dotenv": " (from the .env file)",
            "secrets": " (from the secrets directory)",
        }.get(origin, f" (from {origin.removeprefix('file:')})" if origin else "")
        lines.append(f"  {key or '<root>'}: {err['msg']}{where}")
    return "\n".join(lines)


def load_settings(
    settings_cls: type[AutoAirBaseSettings], config_files: Sequence[str] = (), **values: Any
) -> Any:
    """``settings_cls(**values)`` with extra configuration files (after the environment's)."""
    token = _EXPLICIT_FILES.set(tuple(str(f) for f in config_files))
    try:
        return settings_cls(**values)
    finally:
        _EXPLICIT_FILES.reset(token)


__all__ = [
    "CONFIG_FILE_ENV",
    "AutoAirBaseSettings",
    "ConfigError",
    "ConfigFileSource",
    "add_config_files",
    "configured_files",
    "deep_merge",
    "load_config_file",
    "load_settings",
    "parse_file_list",
]
