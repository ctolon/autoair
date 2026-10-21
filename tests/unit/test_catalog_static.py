"""Static (AST) catalog analysis on synthetic operator modules + disk cache behaviour."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from autoair.catalog import ModulesSource, OperatorCatalog, build_catalog_file, load_catalog_file
from autoair.catalog import service as catalog_service
from autoair.catalog.static import (
    StaticAnalyzer,
    annotation_schema,
    clear_locator_cache,
    parse_module,
)

pytestmark = pytest.mark.airflow  # canonical BaseOperator / BaseSensorOperator sources

BASE = '''
from __future__ import annotations

from typing import TYPE_CHECKING

from airflow.sdk import BaseOperator

if TYPE_CHECKING:
    from airflow.sdk.definitions._internal.types import ArgNotSet

NOTSET = object()


class AcmeHook:
    conn_type = "acme"
    default_conn_name = "acme_default"


class AcmeBase(BaseOperator):
    """Base of acme operators.

    :param a: the A value
        spanning two lines.
    :param acme_conn_id: connection to Acme
    """

    template_fields = ("a",)
    template_fields_renderers = {"a": "sql"}
    ui_color = "#123456"

    def __init__(self, *, a: str, acme_conn_id: str = AcmeHook.default_conn_name, **kwargs):
        super().__init__(**kwargs)
        self.a = a

    def execute(self, context):
        return AcmeHook()
'''

OPS = '''
from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from airflow.providers.common.compat.sdk import BaseSensorOperator

from .base import NOTSET, AcmeBase as Base


def make_base():
    return Base


class FooOperator(Base):
    """Foo does things.

    More text.

    :param b: list of ints
    """

    template_fields = (*Base.template_fields, "b")
    COLOR = "#abcdef"
    ui_color = COLOR

    def __init__(
        self,
        *,
        b: list[int] | None = None,
        mode: Literal["x", "y"] = "x",
        callback: Callable[[], None] | None = None,
        retries_extra=3,
        when: "str | ArgNotSet" = NOTSET,
        mapping: dict[str, float] | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)


class FixedOperator(FooOperator):
    """Supplies ``a`` itself."""

    def __init__(self, **kwargs):
        super().__init__(a="fixed", **kwargs)


class NeedsCallableOperator(Base):
    def __init__(self, *, fn: Callable[..., int], **kwargs):
        super().__init__(**kwargs)


class WaitSensor(BaseSensorOperator):
    template_fields = ("path",)

    def __init__(self, *, path: str, **kwargs):
        super().__init__(**kwargs)


class DynOperator(make_base()):
    def __init__(self, *, z: int = 1, **kwargs):
        super().__init__(a="dyn", **kwargs)


class Helper:
    pass


class _Private(Base):
    pass
'''


@pytest.fixture
def acme(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    pkg = tmp_path / "acme_ops"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "base.py").write_text(textwrap.dedent(BASE))
    (pkg / "ops.py").write_text(textwrap.dedent(OPS))
    monkeypatch.syspath_prepend(str(tmp_path))
    # the import fallback runs in a subprocess: make the package importable there too
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    clear_locator_cache()
    yield "acme_ops.ops"
    clear_locator_cache()
    for name in [m for m in sys.modules if m.startswith("acme_ops")]:
        del sys.modules[name]


@pytest.mark.parametrize(
    ("annotation", "expected", "supported"),
    [
        ("str", {"type": "string"}, True),
        ("int | None", {"anyOf": [{"type": "integer"}, {"type": "null"}]}, True),
        ("Optional[float]", {"anyOf": [{"type": "number"}, {"type": "null"}]}, True),
        ("list[str]", {"type": "array", "items": {"type": "string"}}, True),
        ("Sequence[int]", {"type": "array", "items": {"type": "integer"}}, True),
        ("Iterable", {"type": "array", "items": {}}, True),
        ("dict[str, Any]", {"type": "object"}, True),
        ("dict[str, str]", {"type": "object", "additionalProperties": {"type": "string"}}, True),
        ('Literal["a", "b"]', {"enum": ["a", "b"], "type": "string"}, True),
        ("bool | ArgNotSet", {"type": "boolean"}, True),
        ("str | SomeClass", {}, True),
        ("timedelta", {}, True),
        # Callables are Python references: optional ones stay configurable (None or a ref)…
        ("Callable[..., Any] | None", {"type": "null", "x-autoair-python-ref": "optional"}, True),
        ("str | Callable", {"type": "string", "x-autoair-python-ref": "optional"}, True),
        # …a bare callable can only be a reference.
        ("Callable[[str], int]", {}, False),
        ("'str'", {"type": "string"}, True),
        ("tuple[int, ...]", {"type": "array", "items": {"type": "integer"}}, True),
    ],
)
def test_annotation_schema(annotation, expected, supported):
    assert annotation_schema(annotation) == (expected, supported)


def test_parse_module_records(acme):
    info = parse_module(acme)
    assert info.error is None
    assert info.imports["Base"] == "acme_ops.base.AcmeBase"
    foo = info.classes["FooOperator"]
    assert foo.bases == ["Base"]
    assert [p.name for p in foo.init or []][:3] == ["self", "b", "mode"]
    assert info.classes["FixedOperator"].super_kwargs == ["a"]
    assert "acme_ops" not in sys.modules  # nothing was imported


def test_static_analysis_of_synthetic_module(acme):
    analyzer = StaticAnalyzer()
    mod = analyzer.module(acme)
    assert mod is not None
    r = analyzer.analyze(mod.classes["FooOperator"])
    schema = r.json_schema("FooOperator")
    props = schema["properties"]
    # own params first, then inherited ones (walk stops at BaseOperator)
    assert list(props)[:6] == ["b", "mode", "callback", "retries_extra", "when", "mapping"]
    assert {"a", "acme_conn_id"} <= set(props)
    assert "task_id" not in props
    assert "retries" not in props
    assert schema["required"] == ["a"]  # NOTSET default -> optional
    assert r.template_fields == ["a", "b"]
    assert r.ui_color == "#abcdef"
    assert props["a"]["x-autoair-templated"] is True
    assert props["a"]["x-autoair-renderer"] == "sql"
    assert props["a"]["description"] == "the A value spanning two lines."
    assert props["b"] == {
        "anyOf": [{"type": "array", "items": {"type": "integer"}}, {"type": "null"}],
        "title": "B",
        "description": "list of ints",
        "default": None,
        "x-autoair-templated": True,
        "x-autoair-annotation": "list[int] | None",
    }
    assert props["mode"]["enum"] == ["x", "y"]
    assert props["mode"]["default"] == "x"
    assert props["callback"]["x-autoair-python-ref"]
    assert props["retries_extra"] == {"title": "Retries extra", "default": 3, "type": "number"}
    assert props["acme_conn_id"]["default"] == "acme_default"
    assert props["acme_conn_id"]["x-autoair-widget"] == "connection"
    assert [h.name for h in r.hook_refs] == ["AcmeHook"]
    assert r.summary == "Foo does things."


def test_subclass_supplied_kwargs_are_hidden(acme):
    analyzer = StaticAnalyzer()
    mod = analyzer.module(acme)
    assert mod is not None
    r = analyzer.analyze(mod.classes["FixedOperator"])
    assert "a" not in r.params
    assert "required" not in r.json_schema("FixedOperator")


def test_sensor_params_come_from_base_sensor(acme):
    analyzer = StaticAnalyzer()
    mod = analyzer.module(acme)
    assert mod is not None
    r = analyzer.analyze(mod.classes["WaitSensor"])
    assert r.is_sensor
    assert {"path", "poke_interval", "timeout", "mode", "soft_fail"} <= set(r.params)
    assert r.ui_color == "#e6f1f2"


def _catalog(acme: str, tmp_path: Path, **kw) -> OperatorCatalog:
    return OperatorCatalog([ModulesSource([acme], provider="acme")], cache_dir=tmp_path / "c", **kw)


def test_catalog_on_synthetic_module_without_fallback(acme, tmp_path):
    cat = _catalog(acme, tmp_path, subprocess_fallback=False, use_cache=False)
    names = sorted(s.class_name for s in cat.summaries())
    assert names == ["FixedOperator", "FooOperator", "NeedsCallableOperator", "WaitSensor"]
    foo = cat.get(f"{acme}.FooOperator")
    assert foo is not None
    assert foo.connection_types == ["acme"]  # via the AcmeHook referenced by AcmeBase
    assert foo.params_schema["properties"]["acme_conn_id"]["x-autoair-conn-type"] == ["acme"]
    assert cat.get(f"{acme}.WaitSensor").category == "sensor"  # type: ignore[union-attr]
    needs = cat.get(f"{acme}.NeedsCallableOperator")
    assert needs is not None
    assert needs.builder_supported is True  # via a Python reference
    assert needs.requires_python_code is True
    assert needs.python_ref_params
    assert any("DynOperator" in e.error for e in cat.stats().errors)
    assert "acme_ops" not in sys.modules
    assert cat.validate_params(f"{acme}.FooOperator", {"a": "x", "mode": "z"}) == [
        ("mode", "invalid value for 'mode': 'z' is not one of ['x', 'y']")
    ]
    assert cat.validate_params(f"{acme}.FooOperator", {"a": "x", "b": [1, "2"]})
    assert cat.validate_params(f"{acme}.FooOperator", {"a": "{{ ds }}", "b": [1, 2]}) == []
    assert cat.validate_params(f"{acme}.FixedOperator", {}) == []


def test_unresolvable_class_falls_back_to_subprocess_import(acme, tmp_path):
    cat = _catalog(acme, tmp_path, use_cache=False)
    dyn = cat.get(f"{acme}.DynOperator")
    assert dyn is not None
    assert "z" in dyn.params_schema["properties"]
    assert "a" not in dyn.params_schema["properties"]  # supplied to super().__init__
    assert "acme_ops" not in sys.modules  # imported in the subprocess only


def test_cache_hit_and_miss(acme, tmp_path, monkeypatch):
    first = _catalog(acme, tmp_path)
    s1 = first.stats()
    assert s1.loaded_from_cache is False
    assert list((tmp_path / "c").glob("catalog-static-*.json"))
    second = _catalog(acme, tmp_path)
    s2 = second.stats()
    assert s2.loaded_from_cache is True
    assert s2.cache_key == s1.cache_key
    assert [s.fqn for s in second.summaries()] == [s.fqn for s in first.summaries()]
    # validation works from cached schemas (no pydantic adapters)
    assert second.validate_params(f"{acme}.FooOperator", {"a": 1})

    real = catalog_service.airflow_distributions
    monkeypatch.setattr(
        catalog_service,
        "airflow_distributions",
        lambda: [*real(), ("apache-airflow-providers-fake", "9.9.9")],
    )
    third = _catalog(acme, tmp_path)
    s3 = third.stats()
    assert s3.loaded_from_cache is False
    assert s3.cache_key != s1.cache_key

    # a different strategy is cached separately
    assert first.cache_key_parts()["strategy"] == "static"


def test_cache_invalidated_by_custom_module_change(acme, tmp_path):
    s1 = _catalog(acme, tmp_path).stats()
    path = tmp_path / "acme_ops" / "ops.py"
    path.write_text(path.read_text() + "\n\nclass ExtraOperator(Base):\n    pass\n")
    clear_locator_cache()
    cat = _catalog(acme, tmp_path)
    assert cat.stats().cache_key != s1.cache_key
    assert f"{acme}.ExtraOperator" in cat


def test_build_and_load_catalog_file(acme, tmp_path):
    out = tmp_path / "catalog.json"
    sources = [ModulesSource([acme], provider="acme")]
    stats = build_catalog_file(out, sources)
    assert stats.operators >= 4
    data = json.loads(out.read_text())
    assert data["key"]
    assert data["operators"]
    cat = load_catalog_file(out, sources)
    cat.cache_dir = tmp_path / "unused"
    assert cat.stats().loaded_from_cache is True
    assert f"{acme}.FooOperator" in cat
    assert not (tmp_path / "unused").exists()


def test_static_build_never_imports_airflow(tmp_path):
    code = textwrap.dedent(
        f"""
        import sys
        from pathlib import Path
        from autoair.catalog import OperatorCatalog
        cat = OperatorCatalog(cache_dir=Path({str(tmp_path)!r}), use_cache=False)
        assert cat.get("airflow.providers.standard.operators.bash.BashOperator") is not None
        bad = sorted(m for m in sys.modules if m == "airflow" or m.startswith("airflow."))
        print(bad)
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=120
    )
    assert proc.stdout.strip() == "[]"
