"""Catalog presentation: connection-parameter patterns, icon rules, palette groups, parameter
overrides, operator metadata shipped by packages (autoair.yaml / entry points) and the catalog
diagnostics. A fake ``bifrost`` distribution (``.dist-info`` + package) is installed into a
temporary site directory."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from typer.testing import CliRunner

from autoair.catalog import OperatorCatalog
from autoair.catalog.discovery import DistributionsSource
from autoair.catalog.metadata import static_entry_point_value
from autoair.catalog.static import clear_locator_cache
from autoair.icons import IconError, IconStore, normalize_icon_ref, sanitize_svg
from autoair.sections import CatalogSettings, IconSettings

pytestmark = pytest.mark.airflow

INGEST = '''
from __future__ import annotations

from collections.abc import Sequence

from airflow.sdk import BaseOperator


class BifrostIngestOperator(BaseOperator):
    """Load a dataset into Bifrost.

    :param query: Query selecting the rows.
    :param target_dsn: Bifrost endpoint (a connection id).
    :param bifrost_conn_id: Bifrost connection.
    :param mode: Write mode.
    :param api_token: Token.
    """

    template_fields: Sequence[str] = ("query",)

    def __init__(
        self,
        *,
        query: str,
        target_dsn: str = "bifrost_default",
        bifrost_conn_id: str = "bifrost_default",
        mode: str = "append",
        api_token: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.query = query

    def execute(self, context):
        return self.query


class BifrostPurgeOperator(BaseOperator):
    def __init__(self, *, dataset: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.dataset = dataset

    def execute(self, context):
        return self.dataset


class InternalHelperOperator(BaseOperator):
    def execute(self, context):
        return None
'''

ODIN = """
from airflow.sdk import BaseOperator


class OdinRunOperator(BaseOperator):
    def __init__(self, *, job: str, odin_conn_id: str = "odin_default", **kwargs) -> None:
        super().__init__(**kwargs)
        self.job = job

    def execute(self, context):
        return self.job
"""

ODIN_META = '''
def metadata():
    """Operator metadata for AutoAir."""
    return {
        "group": "Odin",
        "group_order": 5,
        "icon": "clock",
        "operators": {
            "OdinRunOperator": {
                "description": "Run an Odin job.",
                "params": {"job": {"widget": "enum", "enum": ["daily", "hourly"]}},
            }
        },
    }
'''

BIFROST_YAML = """
group: Bifrost
group_order: 10
icon: logo
docs_url: https://docs.bifrost.example/operators
icons:
  logo: icons/bifrost.svg
operators:
  bifrost.operators.ingest.BifrostIngestOperator:
    description: Ingest a dataset into Bifrost.
    docs_url: https://docs.bifrost.example/ingest
    params:
      query: {widget: sql, label: Query, help: The rows to load.}
      mode: {widget: enum, enum: [append, overwrite], default: append}
      api_token: {widget: secret}
    examples:
      - title: Daily load
        params: {query: "select * from sales", mode: append}
  "*Purge*":
    icon: builtin:jira
"""

SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" onload="alert(1)">'
    '<script>alert(1)</script><a href="https://evil"><rect width="1"/></a>'
    '<path d="M0 0h24v24H0z"/>'
    '<circle cx="12" cy="12" r="6" fill="url(#g)" style="fill:url(https://evil/x)"/>'
    "<foreignObject><div>x</div></foreignObject></svg>"
)


def _dist_info(site: Path, name: str, version: str, files: list[str], eps: str = "") -> None:
    info = site / f"{name.replace('-', '_')}-{version}.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
        "Requires-Dist: apache-airflow>=3.0\n"
    )
    record = [*files, f"{info.name}/METADATA,,", f"{info.name}/RECORD,,"]
    if eps:
        (info / "entry_points.txt").write_text(textwrap.dedent(eps))
        record.append(f"{info.name}/entry_points.txt,,")
    (info / "RECORD").write_text("\n".join(f if "," in f else f"{f},," for f in record) + "\n")
    (info / "top_level.txt").write_text(files[0].split("/", 1)[0] + "\n")


@pytest.fixture
def site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    site = tmp_path / "site"
    pkg = site / "bifrost"
    (pkg / "operators").mkdir(parents=True)
    (pkg / "icons").mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "operators" / "__init__.py").write_text("")
    (pkg / "operators" / "ingest.py").write_text(textwrap.dedent(INGEST))
    (pkg / "autoair.yaml").write_text(BIFROST_YAML)
    (pkg / "icons" / "bifrost.svg").write_text(SVG)
    _dist_info(
        site,
        "bifrost",
        "1.2.0",
        [
            "bifrost/__init__.py",
            "bifrost/operators/__init__.py",
            "bifrost/operators/ingest.py",
            "bifrost/autoair.yaml",
            "bifrost/icons/bifrost.svg",
        ],
    )
    odin = site / "odin_ops"
    odin.mkdir()
    (odin / "__init__.py").write_text("")
    (odin / "run.py").write_text(textwrap.dedent(ODIN))
    (odin / "meta.py").write_text(textwrap.dedent(ODIN_META))
    _dist_info(
        site,
        "odin-ops",
        "0.3.0",
        ["odin_ops/__init__.py", "odin_ops/run.py", "odin_ops/meta.py"],
        eps="""
        [autoair.operator_metadata]
        odin = odin_ops.meta:metadata
        """,
    )
    monkeypatch.syspath_prepend(str(site))
    monkeypatch.setenv("PYTHONPATH", str(site))  # the import probe subprocess
    clear_locator_cache()
    yield site
    clear_locator_cache()


def catalog(presentation: CatalogSettings | None = None, **kwargs) -> OperatorCatalog:
    return OperatorCatalog(
        [DistributionsSource(["bifrost", "odin-ops"])],
        use_cache=False,
        presentation=presentation or CatalogSettings(),
        **kwargs,
    )


# --------------------------------------------------------------------------- 1. connections


def test_connection_patterns_default_extra_and_override(site):
    default = catalog()
    props = default.get("bifrost.operators.ingest.BifrostIngestOperator").params_schema[
        "properties"
    ]
    assert props["bifrost_conn_id"]["x-autoair-widget"] == "connection"
    assert "x-autoair-widget" not in props["target_dsn"]

    extra = catalog(
        CatalogSettings(
            connection_param_patterns_extra=[{"pattern": "_dsn$", "conn_type": "bifrost"}]
        )
    )
    detail = extra.get("bifrost.operators.ingest.BifrostIngestOperator")
    props = detail.params_schema["properties"]
    assert props["target_dsn"]["x-autoair-widget"] == "connection"
    assert props["target_dsn"]["x-autoair-conn-type"] == ["bifrost"]
    assert "bifrost" in detail.connection_types

    replaced = catalog(CatalogSettings(connection_param_patterns=["_dsn$"]))
    props = replaced.get("bifrost.operators.ingest.BifrostIngestOperator").params_schema[
        "properties"
    ]
    assert "x-autoair-widget" not in props["bifrost_conn_id"]  # no longer *_conn_id
    assert props["target_dsn"]["x-autoair-widget"] == "connection"
    # The patterns are part of the cache key: a change never serves a stale catalog.
    assert default.cache_key_parts() != replaced.cache_key_parts()


def test_connection_patterns_are_validated_at_load():
    with pytest.raises(ValidationError, match=r"connection_param_patterns\[1\].*invalid regular"):
        CatalogSettings(connection_param_patterns=["_conn_id$", "(unclosed"])
    with pytest.raises(ValidationError, match="invalid regular expression"):
        CatalogSettings(connection_param_patterns_extra=[{"pattern": "[x"}])
    assert [p.pattern for p in CatalogSettings().connection_patterns()] == [
        r"(^|_)conn(ection)?_id$"
    ]


# --------------------------------------------------------------------------- 2. icons


def test_icon_rules_validation_and_precedence(tmp_path):
    assert normalize_icon_ref("agent").startswith("builtin:glyph-agent")
    with pytest.raises(ValueError, match="unknown icon"):
        normalize_icon_ref("no-such-glyph")
    with pytest.raises(ValueError, match="unknown bundled icon"):
        normalize_icon_ref("builtin:does-not-exist")
    with pytest.raises(ValidationError, match="invalid regular expression"):
        IconSettings(rules_extra=[{"match": "(", "icon": "agent"}])
    rules = IconSettings(
        rules_extra=[
            {"match": r"^bifrost", "icon": "builtin:jira"},
            {"match": r"odin", "icon": "custom:missing"},
        ]
    ).rules_extra
    compiled = tuple((r.match, r.icon) for r in rules)
    store = IconStore(str(tmp_path / "icons"))
    common = {"class_name": "BifrostIngestOperator", "module": "bifrost.ops", "integration": None}
    # Rule > package-declared icon > inference.
    assert (
        store.resolve(
            fqn="bifrost.ops.BifrostIngestOperator",
            provider="bifrost",
            declared="package:bifrost-logo",
            rules=compiled,
            **common,
        )
        == "builtin:jira"
    )
    assert (
        store.resolve(
            fqn="x.ops.BifrostIngestOperator",
            provider="x",
            declared="package:bifrost-logo",
            rules=compiled,
            **common,
        )
        == "package:bifrost-logo"
    )
    # An unknown uploaded icon is ignored (falls through to inference).
    assert (
        store.resolve(fqn="odin.X", provider="odin", rules=compiled, **common) != "custom:missing"
    )
    # An assignment (admin UI) still wins over everything.
    store.assign(operator="bifrost.ops.BifrostIngestOperator", icon="builtin:dbt")
    assert (
        store.resolve(
            fqn="bifrost.ops.BifrostIngestOperator", provider="bifrost", rules=compiled, **common
        )
        == "builtin:dbt"
    )


def test_sanitize_svg_keeps_only_static_art():
    clean = sanitize_svg(SVG)
    for bad in ("script", "onload", "foreignObject", "https://evil", "<a"):
        assert bad not in clean
    assert "<path" in clean
    assert "<circle" in clean
    assert 'fill="url(#g)"' in clean
    assert 'viewBox="0 0 24 24"' in clean  # SVG names are case-sensitive
    with pytest.raises(IconError, match="DOCTYPE"):
        sanitize_svg('<!DOCTYPE svg [<!ENTITY x "y">]><svg/>')
    with pytest.raises(IconError, match="root element"):
        sanitize_svg("<html/>")
    with pytest.raises(IconError, match="larger"):
        sanitize_svg("<svg>" + " " * 5000 + "</svg>", max_bytes=1024)


# --------------------------------------------------------------------------- 3-5. metadata


def test_package_metadata_file_and_static_entry_point(site):
    cat = catalog()
    ingest = cat.get("bifrost.operators.ingest.BifrostIngestOperator")
    assert ingest.group == "Bifrost"
    assert ingest.group_order == 10
    assert ingest.summary == "Ingest a dataset into Bifrost."
    assert ingest.docs_url == "https://docs.bifrost.example/ingest"
    assert ingest.icon == "package:bifrost-logo"
    props = ingest.params_schema["properties"]
    assert props["query"]["x-autoair-widget"] == "sql"
    assert props["query"]["x-autoair-renderer"] == "sql"
    assert props["query"]["title"] == props["query"]["x-autoair-label"] == "Query"
    assert props["mode"]["enum"] == ["append", "overwrite"]
    assert props["api_token"]["x-autoair-widget"] == "secret"
    assert ingest.examples[0].title == "Daily load"
    # Package-wide defaults plus a pattern entry.
    purge = cat.get("bifrost.operators.ingest.BifrostPurgeOperator")
    assert purge.group == "Bifrost"
    assert purge.icon == "builtin:jira"
    assert purge.docs_url == "https://docs.bifrost.example/operators"
    # The icon is served sanitised from the catalog.
    svg = cat.package_icon("package:bifrost-logo")
    assert svg
    assert "<path" in svg
    assert "script" not in svg
    # The entry point's function was read statically (static strategy: never imported).
    odin = cat.get("odin_ops.run.OdinRunOperator")
    assert odin.group == "Odin"
    assert odin.group_order == 5
    assert odin.icon.startswith("builtin:glyph-clock")
    assert odin.params_schema["properties"]["job"]["enum"] == ["daily", "hourly"]
    import sys

    assert "odin_ops.meta" not in sys.modules
    diag = cat.diagnostics()
    origins = {m.kind: m for m in diag.metadata}
    assert origins["file"].origin.endswith("bifrost/autoair.yaml")
    assert origins["file"].operators == 3
    assert origins["file"].icons == 1
    assert origins["entry_point"].origin == "odin = odin_ops.meta:metadata"
    assert origins["entry_point"].distribution == "odin-ops"


def test_entry_point_with_import_strategy(site):
    cat = catalog(strategy="import")
    assert cat.get("odin_ops.run.OdinRunOperator").group == "Odin"


def test_static_entry_point_needs_a_literal(tmp_path, monkeypatch):
    mod = tmp_path / "dyn_meta.py"
    mod.write_text("import os\n\ndef meta():\n    x = {}\n    return x\n\nLIT = {'group': 'G'}\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    clear_locator_cache()
    from autoair.catalog.static import find_module_file

    assert static_entry_point_value("dyn_meta:LIT", find_module_file) == {"group": "G"}
    with pytest.raises(ValueError, match="cannot be read statically"):
        static_entry_point_value("dyn_meta:meta", find_module_file)


def test_invalid_metadata_is_reported_not_fatal(site):
    (site / "bifrost" / "autoair.yaml").write_text("group: 1\nbogus_key: x\n")
    cat = catalog()
    assert cat.get("bifrost.operators.ingest.BifrostIngestOperator").group is None
    [file_source] = [m for m in cat.diagnostics().metadata if m.kind == "file"]
    assert "bogus_key" in (file_source.error or "")


def test_configuration_wins_over_package_metadata(site):
    cat = catalog(
        CatalogSettings(
            groups=[{"match": r"^bifrost\.", "group": "Data platform", "order": 1}],
            param_overrides={
                "bifrost.*": {"query": {"widget": "python", "required": True}},
                "*.BifrostIngestOperator": {
                    "mode": {"hidden": True},
                    "bifrost_conn_id": {"conn_type": "http"},
                },
            },
        )
    )
    ingest = cat.get("bifrost.operators.ingest.BifrostIngestOperator")
    assert (ingest.group, ingest.group_order) == ("Data platform", 1)
    props = ingest.params_schema["properties"]
    assert props["query"]["x-autoair-widget"] == "python"  # config > package (sql)
    assert props["query"]["title"] == "Query"  # package label kept
    assert props["mode"]["x-autoair-hidden"] is True
    assert props["bifrost_conn_id"]["x-autoair-conn-type"] == ["http"]
    # Overrides drive validation too.
    problems = cat.validate_params(ingest.fqn, {})
    assert any(field == "query" for field, _ in problems)
    # Provider groups get the default order; summaries carry group fields.
    odin = next(s for s in cat.summaries() if s.class_name == "OdinRunOperator")
    assert odin.group == "Odin"


def test_unmatched_operators_keep_provider_group_with_default_order(site):
    cat = catalog(CatalogSettings(package_metadata=False, default_group_order=50))
    s = next(s for s in cat.summaries() if s.class_name == "BifrostIngestOperator")
    assert s.group is None
    assert s.group_order == 50
    assert s.provider == "bifrost"


# --------------------------------------------------------------------------- 6. diagnostics


def test_diagnostics_sources_filters_and_errors(site):
    (site / "bifrost" / "operators" / "broken.py").write_text(
        "from airflow.sdk import BaseOperator\nclass BrokenOperator(BaseOperator:\n"
    )
    record = next(site.glob("bifrost-*.dist-info")) / "RECORD"
    record.write_text(record.read_text() + "bifrost/operators/broken.py,,\n")
    from autoair.catalog import discovery
    from autoair.sections import DiscoverySettings

    filters = discovery.DiscoveryFilters.from_settings(
        DiscoverySettings(skip_class_patterns_extra=["^Internal"])
    )
    cat = OperatorCatalog(
        [DistributionsSource(["bifrost", "odin-ops"])],
        use_cache=False,
        presentation=CatalogSettings(),
        deny=["*Purge*"],
        discovery=filters,
    )
    diag = cat.diagnostics()
    [source] = diag.sources
    assert source.source == "DistributionsSource"
    assert source.detail == ["bifrost", "odin-ops"]
    assert source.modules >= 2
    assert set(source.providers) == {"bifrost", "odin-ops"}
    assert "bifrost.operators.ingest.BifrostIngestOperator" in source.operators
    reasons = {(f.name.rsplit(".", 1)[-1], f.reason) for f in diag.filtered}
    assert ("BifrostPurgeOperator", "operator_deny") in reasons
    assert ("InternalHelperOperator", "discovery.skip_class_pattern") in reasons
    assert any(e.module == "bifrost.operators.broken" for e in diag.parse_errors)
    assert diag.operators == 2
    assert diag.build_seconds >= 0
    assert diag.loaded_at >= diag.built_at


def test_operators_are_credited_to_every_source_yielding_them(site):
    from autoair.catalog.service import ModulesSource

    cat = OperatorCatalog(
        [DistributionsSource(["bifrost"]), ModulesSource(["bifrost.operators.ingest"])],
        use_cache=False,
        presentation=CatalogSettings(),
    )
    fqn = "bifrost.operators.ingest.BifrostIngestOperator"
    assert all(fqn in s.operators for s in cat.diagnostics().sources)


def test_catalog_dump_diagnostics_cli(site, monkeypatch, tmp_path):
    from autoair.cli import app

    monkeypatch.setenv("AUTOAIR_EXTRA_OPERATOR_DISTRIBUTIONS", '["bifrost"]')
    monkeypatch.setenv("AUTOAIR_SCAN_PROVIDER_PACKAGES", "false")
    monkeypatch.setenv("AUTOAIR_DISCOVER_AIRFLOW_FOLDERS", "false")
    monkeypatch.setenv("AUTOAIR_OPERATOR_DENY", '["*Purge*"]')
    monkeypatch.setenv("AUTOAIR_CACHE_DIR", str(tmp_path / "cache"))
    result = CliRunner().invoke(app, ["catalog", "dump", "--diagnostics"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert any(s["source"] == "DistributionsSource" for s in data["sources"])
    assert any(f["reason"] == "operator_deny" for f in data["filtered"])
    assert any(m["origin"].endswith("autoair.yaml") for m in data["metadata"])


def test_diagnostics_endpoint_and_package_icon_route(site, tmp_path):
    from autoair.api.app import build_services, create_app
    from autoair.auth import AuthBackend
    from autoair.settings import Settings

    settings = Settings(
        dags_folder=tmp_path,
        subfolder="",
        ai={"enabled": False},
        forbidden_operators=["*OdinRun*"],
    )
    services = build_services(settings, AuthBackend())
    services.catalog = catalog()
    assert services.icons is not None
    services.icons.package_icons = services.catalog
    client = TestClient(create_app(services))
    r = client.get("/api/catalog/diagnostics")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["operators"] == 4  # ingest, purge, internal helper, odin
    assert any(
        f["reason"] == "policy.forbidden_operators" and f["name"].endswith("OdinRunOperator")
        for f in body["filtered"]
    )
    icon = client.get("/api/icons/package:bifrost-logo")
    assert icon.status_code == 200
    assert icon.headers["content-type"].startswith("image/svg")
    assert b"script" not in icon.content
    ops = {o["class_name"]: o for o in client.get("/api/catalog/operators").json()}
    assert ops["BifrostIngestOperator"]["icon"] == "package:bifrost-logo"
    assert ops["BifrostIngestOperator"]["group"] == "Bifrost"
    assert any(i["id"] == "package:bifrost-logo" for i in client.get("/api/icons").json())
