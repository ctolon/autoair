"""``autoair`` command line interface."""

from __future__ import annotations

import json
import sys
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, NoReturn

import typer

from autoair.spec import DagSpec

if TYPE_CHECKING:
    from autoair.settings import Settings


def _group(help_text: str) -> typer.Typer:
    # Markdown mode reflows wrapped docstrings instead of keeping their line breaks.
    return typer.Typer(help=help_text, no_args_is_help=True, rich_markup_mode="markdown")


app = _group("AutoAir — no-code DAG builder for Apache Airflow 3.")

ConfigOption = Annotated[
    list[str] | None,
    typer.Option(
        "--config",
        "-c",
        help="Configuration file (YAML, JSON, INI, .py[:attr] or module:attr); repeatable. "
        "Added after AUTOAIR_CONFIG_FILE; environment variables still win.",
    ),
]


def _print_version(value: bool) -> None:
    if value:
        from autoair import __version__

        typer.echo(f"autoair {__version__}")
        raise typer.Exit()


@app.callback()
def _main(
    config: ConfigOption = None,
    version: Annotated[
        bool | None,
        typer.Option(
            "--version", callback=_print_version, is_eager=True, help="Print the version and exit."
        ),
    ] = None,
) -> None:
    """AutoAir — no-code DAG builder for Apache Airflow 3."""
    if config:
        from autoair.config_sources import add_config_files

        add_config_files(config)


def _load(path: Path) -> DagSpec:
    return DagSpec.model_validate_json(path.read_text(encoding="utf-8"))


def _settings(**overrides: Any) -> Settings:
    """Settings of this command (configuration errors end the command with exit code 2);
    their sections become active for code that reads them (codegen, lint, limits…)."""
    from autoair import sections
    from autoair.config_sources import ConfigError
    from autoair.settings import Settings

    try:
        settings = Settings(**overrides)
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    sections.activate(settings)
    return settings


def _require_feature(settings: Settings, name: str) -> None:
    if not settings.feature(name):
        typer.echo(f"the {name} feature is disabled (AUTOAIR_FEATURES__{name.upper()})", err=True)
        raise typer.Exit(2)


def _require_http_server(command: str, install: str = "standalone") -> None:
    """End ``command`` with a clear message when uvicorn (the ``standalone`` extra) is
    missing, instead of a ``ModuleNotFoundError`` traceback."""
    import importlib.util

    if importlib.util.find_spec("uvicorn") is not None:
        return
    typer.echo(
        f"`autoair {command}` needs an HTTP server, which comes with the 'standalone' extra "
        f"(uvicorn). Install it with: pip install 'autoair[{install}]'",
        err=True,
    )
    if command == "local":
        typer.echo(
            f"or run it with uv: uvx --from 'autoair[{install}]' autoair local --open",
            err=True,
        )
    raise typer.Exit(1)


@app.command()
def serve(
    host: Annotated[
        str | None, typer.Option(help="Bind address (default AUTOAIR_SERVER__HOST, 127.0.0.1).")
    ] = None,
    port: Annotated[
        int | None, typer.Option(help="Port (default AUTOAIR_SERVER__PORT, 8765).")
    ] = None,
    dags_folder: Annotated[Path | None, typer.Option(help="Overrides AUTOAIR_DAGS_FOLDER.")] = None,
    airflow_url: Annotated[str | None, typer.Option(help="Overrides AUTOAIR_AIRFLOW_URL.")] = None,
    no_auth: Annotated[bool, typer.Option(help="Do not require a bearer token.")] = False,
    workers: Annotated[
        int | None, typer.Option(help="Worker processes (AUTOAIR_SERVER__WORKERS).")
    ] = None,
    ssl_certfile: Annotated[
        Path | None, typer.Option(help="TLS certificate (PEM); enables HTTPS.")
    ] = None,
    ssl_keyfile: Annotated[Path | None, typer.Option(help="TLS private key (PEM).")] = None,
    ssl_ca_certs: Annotated[
        Path | None, typer.Option(help="CA bundle for client certificates (mutual TLS).")
    ] = None,
    ssl_cert_reqs: Annotated[
        str | None, typer.Option(help="Client certificates: none, optional, required.")
    ] = None,
    forwarded_allow_ips: Annotated[
        str | None,
        typer.Option(help="Comma-separated proxy addresses trusted for X-Forwarded-* ('*': all)."),
    ] = None,
    config: ConfigOption = None,
) -> None:
    """Run the builder as a standalone service (UI at /ui/, API at /api).

    TLS, proxy, worker and security-header options are AUTOAIR_SERVER__* settings
    (docs/deployment.md). The TLS key password is read only from
    AUTOAIR_SERVER__SSL_KEYFILE_PASSWORD."""
    from autoair.server import ServeOptions, run

    _require_http_server("serve")
    if config:
        from autoair.config_sources import add_config_files

        add_config_files(config)  # also seen by subprocesses (catalog build, checks…)
    overrides: dict[str, Any] = {"mode": "standalone"}
    if dags_folder is not None:
        overrides["dags_folder"] = str(dags_folder)
    if airflow_url is not None:
        overrides["airflow_url"] = airflow_url
    server: dict[str, Any] = {}
    for key, value in (
        ("workers", workers),
        ("ssl_certfile", ssl_certfile),
        ("ssl_keyfile", ssl_keyfile),
        ("ssl_ca_certs", ssl_ca_certs),
        ("ssl_cert_reqs", ssl_cert_reqs),
    ):
        if value is not None:
            server[key] = str(value) if isinstance(value, Path) else value
    if forwarded_allow_ips is not None:
        server["forwarded_allow_ips"] = [i.strip() for i in forwarded_allow_ips.split(",")]
    if server:
        overrides["server"] = server
    settings = _settings(**overrides)
    options = ServeOptions(overrides=overrides, no_auth=no_auth)
    typer.echo(f"AutoAir: writing DAGs to {settings.output_dir}; Airflow at {settings.airflow_url}")
    try:
        run(
            options,
            settings,
            host=host or settings.server.host,
            port=port or settings.server.port,
        )
    except ValueError as exc:  # inconsistent TLS options
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc


@app.command()
def local(
    dags_folder: Annotated[
        Path | None, typer.Option(help="Folder DAGs are written to (default ./dags).")
    ] = None,
    host: Annotated[str, typer.Option(help="Bind address (keep it on loopback).")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port.")] = 8765,
    token: Annotated[
        str | None,
        typer.Option(
            help="Require this access token (also AUTOAIR_LOCAL__TOKEN); paste it in the UI.",
            envvar="AUTOAIR_LOCAL__TOKEN",
        ),
    ] = None,
    open_browser: Annotated[
        bool | None, typer.Option("--open/--no-open", help="Open the UI in the browser.")
    ] = None,
    config: ConfigOption = None,
) -> None:
    """Run AutoAir on a laptop without an Airflow server
    (`uvx --from 'autoair[standalone,airflow]' autoair local`).

    Provides the operator catalog of the installed packages, static validation (plus the
    DagBag import check when Airflow is installed) and deploys to a local folder or git.
    Runs, test runs and Airflow pickers are disabled."""
    from autoair.server import ServeOptions, airflow_installed, is_loopback, local_preset, run

    # The operator palette of local mode comes from the installed Airflow and providers.
    _require_http_server("local", "standalone" if airflow_installed() else "standalone,airflow")
    if config:
        from autoair.config_sources import add_config_files

        add_config_files(config)
    overrides = local_preset(dags_folder=dags_folder, host=host, port=port)
    if token:
        overrides["local"] = {"token": token}
    if open_browser is not None:
        overrides.setdefault("local", {})["open_browser"] = open_browser
    settings = _settings(**overrides)
    if not is_loopback(host) and settings.local.token is None:
        typer.echo(
            f"refusing to serve {host} without --token: local mode has no other authentication",
            err=True,
        )
        raise typer.Exit(2)
    options = ServeOptions(overrides=overrides, local=True)
    typer.echo(f"AutoAir (local mode): http://{host}:{port}/ui/ — DAGs in {settings.output_dir}")
    if settings.local.token is not None:
        typer.echo("Access token required: paste it on the sign-in page.")
    run(options, settings, host=host, port=port, open_browser=settings.local.open_browser)


@app.command()
def generate(
    spec: Path,
    output: Annotated[
        Path | None, typer.Option("-o", "--output", help="Output file (default: stdout).")
    ] = None,
) -> None:
    """Generate DAG Python source from a spec file."""
    from autoair.codegen import render

    _settings()

    code = render(_load(spec))
    if output:
        output.write_text(code, encoding="utf-8")
    else:
        sys.stdout.write(code)


@app.command()
def validate(
    spec: Path,
    import_check: Annotated[
        bool, typer.Option(help="Also import the generated file with Airflow's DagBag.")
    ] = True,
    environment: Annotated[
        str | None, typer.Option("--env", help="Validate for this environment (overlay, rules).")
    ] = None,
) -> None:
    """Validate a spec (catalog, graph, policy and an Airflow DagBag import)."""
    from autoair.catalog import OperatorCatalog
    from autoair.validation import policy_from_settings, validate_spec

    settings = _settings()
    loaded = _load(spec)
    env = environment or settings.default_environment
    if env and env in loaded.environments:
        loaded = loaded.with_environment(env)
    from autoair.blueprints.catalog import BlueprintCatalog

    catalog = OperatorCatalog()
    catalog.blueprints = BlueprintCatalog(settings.blueprints)
    report = validate_spec(
        loaded,
        environment=env,
        catalog=catalog,
        import_check=import_check,
        import_timeout=settings.import_check_timeout,
        policy=policy_from_settings(settings),
    )
    for p in report.problems:
        where = f" [{p.node_id}{'.' + p.field if p.field else ''}]" if p.node_id else ""
        typer.echo(f"{p.severity.upper():7} {p.code}{where}: {p.message}")
    typer.echo("OK" if report.ok else "FAILED")
    raise typer.Exit(0 if report.ok else 1)


catalog_app = _group("Operator catalog commands.")
app.add_typer(catalog_app, name="catalog")


@catalog_app.command("dump")
def catalog_dump(
    details: Annotated[
        bool, typer.Option(help="Full operator entries instead of summaries.")
    ] = False,
    diagnostics: Annotated[
        bool,
        typer.Option(
            help="Print the build diagnostics instead: scanned sources with counts, operators "
            "per source, what was filtered out and why, failed modules, applied package "
            "metadata, build time."
        ),
    ] = False,
) -> None:
    """Print the discovered operator catalog as JSON."""
    from autoair.catalog import OperatorCatalog

    if diagnostics:
        from autoair.catalog.discovery import CatalogSourceSettings

        selected = CatalogSourceSettings()  # operator_allow / operator_deny as configured
        filtered = OperatorCatalog(allow=selected.operator_allow, deny=selected.operator_deny)
        typer.echo(filtered.diagnostics().model_dump_json(indent=2))
        return
    cat = OperatorCatalog()
    items = [cat.get(s.fqn) for s in cat.summaries()] if details else cat.summaries()
    typer.echo(json.dumps([i.model_dump() for i in items if i], indent=2))


class CatalogBuildStrategy(str, Enum):
    """``autoair catalog build --strategy`` (the values of ``CatalogStrategy``)."""

    static = "static"
    import_ = "import"


@catalog_app.command("build")
def catalog_build(
    output: Annotated[Path, typer.Option("--output", "-o", help="Output file.")] = Path(
        "autoair-catalog.json"
    ),
    strategy: Annotated[
        CatalogBuildStrategy, typer.Option(case_sensitive=False, help="static or import.")
    ] = CatalogBuildStrategy.static,
) -> None:
    """Precompute the catalog (e.g. in a Docker build); point AUTOAIR_CATALOG_FILE at it."""
    from autoair.catalog.service import build_catalog_file

    stats = build_catalog_file(
        output, strategy="import" if strategy is CatalogBuildStrategy.import_ else "static"
    )
    typer.echo(
        f"wrote {output}: {stats.operators} operators, {stats.providers} providers, "
        f"{len(stats.errors)} errors"
    )


market_app = _group("Marketplace index publishing (Ed25519-signed).")
app.add_typer(market_app, name="marketplace")


@market_app.command("keygen")
def marketplace_keygen(
    private_key: Annotated[
        Path, typer.Option("--out", help="Private key file to write (PEM, mode 0600).")
    ] = Path("marketplace.pem"),
) -> None:
    """Create a signing key; prints the public key to put in AUTOAIR_MARKETPLACE_SOURCES."""
    from autoair.marketplace import generate_keypair

    _require_feature(_settings(), "marketplace")

    pem, public = generate_keypair()
    private_key.write_bytes(pem)
    private_key.chmod(0o600)
    typer.echo(public)


@market_app.command("build")
def marketplace_build(
    directory: Path,
    name: Annotated[str, typer.Option(help="Marketplace name.")],
    key: Annotated[Path | None, typer.Option(help="Ed25519 private key (PEM).")] = None,
    description: Annotated[str, typer.Option(help="Marketplace description.")] = "",
) -> None:
    """Write DIRECTORY/index.json from DIRECTORY/templates/*.json (+ extensions.json), signed."""
    import json

    from autoair.marketplace import build_index

    _require_feature(_settings(), "marketplace")
    index = build_index(directory, name, key.read_bytes() if key else None, description)
    (directory / "index.json").write_text(json.dumps(index, indent=2) + "\n")
    typer.echo(f"{len(index['entries'])} entries → {directory / 'index.json'}")


@market_app.command("verify")
def marketplace_verify(
    index: Path,
    public_key: Annotated[str, typer.Option(help="Base64 Ed25519 public key.")],
) -> None:
    """Check an index signature against a base64 Ed25519 public key."""
    import json

    from autoair.marketplace import verify_index

    if not verify_index(json.loads(index.read_text()), public_key):
        typer.echo("signature INVALID", err=True)
        raise typer.Exit(1)
    typer.echo("signature OK")


@app.command("mcp")
def mcp_server(
    http: Annotated[
        bool, typer.Option(help="Serve unauthenticated streamable HTTP instead of stdio.")
    ] = False,
    host: Annotated[str, typer.Option(help="HTTP bind address.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="HTTP port.")] = 8766,
) -> None:
    """Run the AutoAir MCP server (stdio, or streamable HTTP for local use).

    For a shared endpoint use `autoair mcp-serve --http`, which authenticates callers."""
    from autoair.mcp.server import main

    argv = ["--http", "--host", host, "--port", str(port)] if http else []
    main(argv)


@app.command("mcp-serve")
def mcp_serve(
    http: Annotated[bool, typer.Option(help="Serve streamable HTTP instead of stdio.")] = False,
    host: Annotated[str, typer.Option(help="HTTP bind address.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="HTTP port.")] = 8766,
    no_auth: Annotated[bool, typer.Option(help="HTTP: do not require a bearer token.")] = False,
) -> None:
    """Run a standalone MCP server.

    HTTP mode serves http://HOST:PORT/mcp/ with the same authentication as `autoair serve`:
    the caller's Airflow bearer token, verified against Airflow."""
    if not http:
        from autoair.mcp.server import main

        main([])
        return
    _require_http_server("mcp-serve --http", "standalone,mcp")
    import uvicorn
    from fastapi import FastAPI

    from autoair.api.app import build_services
    from autoair.auth import TokenVerifier, bearer_passthrough
    from autoair.mcp.http import create_mcp_asgi_app
    from autoair.settings import Settings
    from autoair.validation.sandbox import harden_server_process

    settings = Settings(mode="standalone")
    harden_server_process("mcp-serve", enabled=settings.server.harden_process)
    verifier = TokenVerifier(
        settings.airflow_url, ttl=settings.auth_cache_ttl, timeout=settings.auth_timeout
    )
    services = build_services(settings, bearer_passthrough(verifier, required=not no_auth))
    root = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    root.mount("/mcp", create_mcp_asgi_app(services))
    typer.echo(f"AutoAir MCP at http://{host}:{port}/mcp/ (Airflow at {settings.airflow_url})")
    uvicorn.run(root, host=host, port=port)


@app.command(
    "docs-mcp",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    add_help_option=False,
)
def docs_mcp(ctx: typer.Context) -> None:
    """Serve AutoAir's own documentation over MCP for coding agents (stdio by default).

    Only the documentation tools (search_autoair_docs, read_autoair_doc, list_autoair_docs)
    over the pages packaged with this version: no Airflow, settings, state or network.
    Options: --http, --host, --port, --path; `autoair docs-mcp --help` shows them with setup
    snippets for Claude Code, Codex CLI and Cursor. Needs autoair[mcp]
    (`uvx --from 'autoair[mcp]' autoair docs-mcp`)."""
    from autoair.docs.mcp import main

    main(list(ctx.args))


@app.command("ai-serve")
def ai_serve(
    host: Annotated[str, typer.Option(help="Bind address.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port.")] = 8790,
    no_auth: Annotated[bool, typer.Option(help="Do not require a bearer token.")] = False,
) -> None:
    """Run the AI service on its own (AI API at /api/ai, MCP at /mcp).

    Point the main server at it with AUTOAIR_AI__SERVICE_URL=http://HOST:PORT."""
    _require_http_server("ai-serve")
    from autoair.ai.availability import ai_unavailable_reason

    reason = ai_unavailable_reason()
    if reason is not None:
        typer.echo(f"autoair ai-serve: {reason}", err=True)
        raise typer.Exit(1)
    import uvicorn

    from autoair.ai.service import build_ai_service
    from autoair.settings import Settings
    from autoair.validation.sandbox import harden_server_process

    harden_server_process("ai-serve", enabled=Settings(mode="standalone").server.harden_process)
    typer.echo(f"AutoAir AI service on http://{host}:{port} (API /api/ai, MCP /mcp)")
    uvicorn.run(build_ai_service(no_auth=no_auth), host=host, port=port)


@app.command("validator-serve")
def validator_serve(
    host: Annotated[str, typer.Option(help="Bind address.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port.")] = 8791,
    no_auth: Annotated[
        bool,
        typer.Option(
            help="Accept unsigned checks (tests only: anyone who reaches the port can run code)."
        ),
    ] = False,
    config: ConfigOption = None,
) -> None:
    """Run the validator service: DagBag import checks of untrusted DAG code in sandboxed
    worker processes (POST /v1/check, /health, /ready, /metrics).

    Point the main server at it with AUTOAIR_VALIDATOR__URL=http://HOST:PORT. Both sides
    share an HMAC key: the password of the Airflow connection validator.secret_conn_id,
    given to this service as AIRFLOW_CONN_<CONN_ID>."""
    _require_http_server("validator-serve")
    if config:
        from autoair.config_sources import add_config_files

        add_config_files(config)
    settings = _settings()
    import importlib.util

    if importlib.util.find_spec("airflow") is None:
        typer.echo(
            "autoair validator-serve: Apache Airflow is not installed (pip install "
            "'autoair[airflow,standalone]' plus the providers your DAGs use)",
            err=True,
        )
        raise typer.Exit(1)
    cfg = settings.validator
    if no_auth and host not in ("127.0.0.1", "localhost", "::1"):
        typer.echo(
            "autoair validator-serve: --no-auth is only allowed on a loopback --host", err=True
        )
        raise typer.Exit(2)
    from autoair.validation.remote import connection_env_name, password_from_env

    if not no_auth and not password_from_env(cfg.secret_conn_id):
        typer.echo(
            f"autoair validator-serve: no HMAC key; set {connection_env_name(cfg.secret_conn_id)} "
            "(an Airflow connection with a password, JSON or URI form) or pass --no-auth "
            "for tests",
            err=True,
        )
        raise typer.Exit(2)
    import uvicorn

    from autoair.logging_setup import configure_logging
    from autoair.validation.service import create_validator_app, harden_process

    configure_logging(settings.logging)
    harden_process()
    typer.echo(f"AutoAir validator on http://{host}:{port} (POST /v1/check)")
    uvicorn.run(
        create_validator_app(cfg, no_auth=no_auth),
        host=host,
        port=port,
        log_config=None,
    )


@app.command("import-python")
def import_python(
    path: Path,
    output_dir: Annotated[
        Path, typer.Option("-o", "--output-dir", help="Folder for the *.autoair.json specs.")
    ] = Path("."),
) -> None:
    """Convert a hand-written Python DAG file into AutoAir spec(s)."""
    from autoair.importer.python_dag import import_python_file

    settings = _settings()
    _require_feature(settings, "imports")
    _require_feature(settings, "python_code")  # importing executes the file
    result = import_python_file(path, timeout=settings.python_import_timeout)
    for err in result.import_errors:
        typer.echo(f"import error: {err}", err=True)
    for dag in result.dags:
        for w in dag.warnings:
            typer.echo(f"{dag.dag_id}: warning: {w}", err=True)
        if dag.spec is None:
            typer.echo(f"{dag.dag_id}: failed: {dag.error}", err=True)
            continue
        target = output_dir / f"{dag.dag_id}.autoair.json"
        target.write_text(dag.spec.model_dump_json(indent=2, exclude_none=True), encoding="utf-8")
        typer.echo(f"wrote {target}")


@app.command("export-yaml")
def export_yaml(spec: Path) -> None:
    """Export a spec as an Airflow 3.4 native YAML Dag."""
    from autoair.codegen.yaml_dag import export_yaml_dag

    _settings()

    result = export_yaml_dag(_load(spec))
    for issue in result.issues:
        typer.echo(f"{issue.severity}: {issue.message}", err=True)
    sys.stdout.write(result.yaml)
    if not result.lossless:
        raise typer.Exit(1)


@app.command("import-yaml")
def import_yaml(path: Path) -> None:
    """Convert an Airflow 3.4 YAML Dag into an AutoAir spec (printed as JSON)."""
    from autoair.codegen.yaml_dag import import_yaml_dag

    _require_feature(_settings(), "imports")

    spec, issues = import_yaml_dag(path.read_text(encoding="utf-8"))
    for issue in issues:
        typer.echo(f"{issue.severity}: {issue.message}", err=True)
    sys.stdout.write(spec.model_dump_json(indent=2, exclude_none=True) + "\n")


@app.command("new-node")
def new_node(
    name: Annotated[str, typer.Argument(help="Node kind name, e.g. http_get.")],
    title: Annotated[str | None, typer.Option(help="Palette title.")] = None,
    description: Annotated[str | None, typer.Option(help="Palette description.")] = None,
    category: Annotated[str | None, typer.Option(help="Palette category.")] = None,
    icon: Annotated[str | None, typer.Option(help="`builtin:<logo>` or `custom:<name>`.")] = None,
    output: Annotated[Path, typer.Option("-o", "--output", help="Parent folder.")] = Path("."),
) -> None:
    """Scaffold a Python package that registers a custom node kind.

    The package registers an `autoair.node_kinds` entry point and contains the handler,
    pyproject.toml, a README and tests. See docs/extending.md."""
    from autoair.scaffold import ScaffoldError
    from autoair.scaffold import new_node as scaffold

    values = {
        k: v
        for k, v in {
            "title": title,
            "description": description,
            "category": category,
            "icon": icon,
        }.items()
        if v is not None
    }
    try:
        project = scaffold(name, output, **values)
    except ScaffoldError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(f"created {project}")
    typer.echo(
        f"next: pip install -e '{project}[test]' && pytest {project}  (then restart AutoAir)"
    )


@app.command("export-openapi")
def export_openapi(
    output: Annotated[Path, typer.Option(help="Output file.")] = Path("frontend/openapi.json"),
) -> None:
    """Write the API's OpenAPI document (input for the TypeScript client generator)."""
    from autoair.api.app import build_services, create_api
    from autoair.auth import AuthBackend
    from autoair.settings import Settings

    api = create_api(build_services(Settings(), AuthBackend()))
    output.write_text(json.dumps(api.openapi(), indent=2) + "\n", encoding="utf-8")
    typer.echo(f"wrote {output}")


@app.command("export-schema")
def export_schema(
    output: Annotated[Path, typer.Option(help="Output file.")] = Path("dag-spec.schema.json"),
) -> None:
    """Write the JSON Schema of the DAG spec."""
    output.write_text(json.dumps(DagSpec.model_json_schema(), indent=2) + "\n", encoding="utf-8")
    typer.echo(f"wrote {output}")


@app.command("export-dag-factory")
def export_dag_factory_cmd(spec: Path) -> None:
    """Export a spec to dag-factory YAML."""
    from autoair.codegen.dagfactory import export_dag_factory

    result = export_dag_factory(_load(spec))
    for w in result.warnings:
        typer.echo(f"warning: {w}", err=True)
    sys.stdout.write(result.yaml)


iam_app = _group("AutoAir roles and permissions (see docs/iam.md).")
app.add_typer(iam_app, name="iam")


@iam_app.command("roles")
def iam_roles(as_json: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False) -> None:
    """Print the AutoAir roles and the permissions each one grants."""
    from autoair.iam.model import PERMISSIONS, ROLE_DESCRIPTIONS, role_table
    from autoair.iam.resolver import custom_view_resource
    from autoair.settings import Settings

    iam = Settings().iam
    table = role_table(iam.roles)
    if as_json:
        data = {
            "roles": {name: sorted(perms) for name, perms in table.items()},
            "permissions": {
                p.name: {
                    "description": p.description,
                    "method": p.method,
                    "airflow_resource": custom_view_resource(iam, p.resource),
                }
                for p in PERMISSIONS
            },
            "direct_push_roles": iam.direct_push_roles,
        }
        typer.echo(json.dumps(data, indent=2))
        return
    for name, perms in table.items():
        typer.echo(f"{name}: {ROLE_DESCRIPTIONS.get(name, '(custom)')}")
        typer.echo("  " + ", ".join(sorted(perms)))
    typer.echo(f"deploy:direct_push: roles {iam.direct_push_roles} (storage.git.direct_push_roles)")


@iam_app.command("sync-fab")
def iam_sync_fab(
    create_roles: Annotated[
        bool, typer.Option(help="Also create `AutoAir <Role>` roles holding the permissions.")
    ] = False,
) -> None:
    """Create AutoAir's permissions in Airflow's FAB auth manager (run where Airflow runs)."""
    from airflow.providers.fab.auth_manager.cli_commands.utils import get_application_builder

    from autoair.iam.fab import sync_fab
    from autoair.settings import Settings

    iam = Settings().iam
    if create_roles and not iam.role_prefix.strip():
        raise typer.BadParameter(
            "iam.role_prefix is empty: the roles would be Airflow's own built-in roles "
            "(Admin, Viewer…); set a prefix such as 'AutoAir '",
            param_hint="--create-roles",
        )
    with get_application_builder() as appbuilder:
        for line in sync_fab(appbuilder.sm, iam, create_roles=create_roles):
            typer.echo(line)
    typer.echo("done")


config_app = _group("Configuration (sources, effective values).")
app.add_typer(config_app, name="config")


def _kb_group() -> None:
    from autoair.kb.cli import kb_app

    app.add_typer(kb_app, name="kb")


_kb_group()


@config_app.command("show")
def config_show(
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
    changed: Annotated[bool, typer.Option(help="Only values that are not defaults.")] = False,
) -> None:
    """Print every effective setting with its source (secrets masked)."""
    from autoair.config_reference import MASK, iter_options, masked, value_at
    from autoair.config_sources import configured_files

    settings = _settings()
    rows = []
    for opt in iter_options(type(settings)):
        source = settings.origin_of(opt.key)
        if changed and source == "default":
            continue
        value = value_at(settings, opt.key)
        shown = (MASK if value not in (None, "", {}, []) else None) if opt.secret else masked(value)
        rows.append({"key": opt.key, "env": opt.env, "value": shown, "source": source})
    if as_json:
        typer.echo(json.dumps({"files": configured_files(), "options": rows}, indent=2))
        return
    for f in configured_files():
        typer.echo(f"# config file: {f}")
    for row in rows:
        typer.echo(f"{row['key']} = {json.dumps(row['value'])}  [{row['source']}]")


db_app = _group("AutoAir's state database (database.backend sqlite/postgresql).")
app.add_typer(db_app, name="db")


def _database(settings: Settings) -> Any:
    from autoair.persistence.db import Database, DatabaseError

    if not settings.database.is_sql:
        typer.echo("database.backend is 'file': nothing to migrate", err=True)
        raise typer.Exit(2)
    try:
        return Database(settings.database, default_sqlite_path=settings.state_dir / "autoair.db")
    except DatabaseError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc


def _db_failed(db: Any, exc: Exception) -> NoReturn:
    from autoair.config_reference import mask_url_credentials

    typer.echo(f"{db.masked_url}: {mask_url_credentials(str(exc)).splitlines()[0]}", err=True)
    raise typer.Exit(1) from exc


@db_app.command("upgrade")
def db_upgrade(
    revision: Annotated[
        int | None, typer.Option(help="Target schema version (default: the latest).")
    ] = None,
) -> None:
    """Create or migrate AutoAir's tables (safe to run from several replicas at once)."""
    from autoair.persistence import migrations

    db = _database(_settings())
    try:
        applied = migrations.upgrade(db, revision)
    except migrations.MigrationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    except Exception as exc:
        _db_failed(db, exc)
    finally:
        db.dispose()
    typer.echo(f"applied: {applied or 'nothing (up to date)'}; head: {migrations.HEAD}")


@db_app.command("current")
def db_current(
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
) -> None:
    """Print the applied schema version, the latest one and pending migrations."""
    from autoair.persistence import migrations

    db = _database(_settings())
    try:
        current = migrations.current(db)
        pending = [m.version for m in migrations.pending(db)]
    except Exception as exc:
        _db_failed(db, exc)
    finally:
        db.dispose()
    info = {"url": db.masked_url, "current": current, "head": migrations.HEAD, "pending": pending}
    if as_json:
        typer.echo(json.dumps(info))
        return
    typer.echo(f"{db.masked_url}: version {current} (head {migrations.HEAD})")
    if pending:
        typer.echo(f"pending: {pending} - run `autoair db upgrade`")
