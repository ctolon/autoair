"""``autoair kb``: the documentation knowledge base from the command line."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

if TYPE_CHECKING:
    from autoair.kb.service import KnowledgeBase
    from autoair.settings import Settings

kb_app = typer.Typer(
    help="Documentation knowledge base (Airflow, providers, Astronomer): sources, refresh, "
    "search, portable bundles for air-gapped installs.",
    no_args_is_help=True,
    rich_markup_mode="markdown",
)

JsonOption = Annotated[bool, typer.Option("--json", help="Print JSON.")]
SourceOption = Annotated[
    list[str] | None, typer.Option("--source", "-s", help="Only this source id (repeatable).")
]


def _settings(**overrides: Any) -> Settings:
    from autoair import cli

    settings = cli._settings(**overrides)
    cli._require_feature(settings, "kb")
    return settings


def _kb(settings: Settings | None = None, **kwargs: Any) -> KnowledgeBase:
    from autoair.kb.service import KnowledgeBase

    return KnowledgeBase(settings or _settings(), **kwargs)


def _echo(data: Any) -> None:
    typer.echo(json.dumps(data, indent=2, default=str))


def _run_summary(status: Any) -> None:
    typer.echo(f"outcome: {status.outcome}  ({status.started_at} -> {status.finished_at})")
    for run in status.sources:
        typer.echo(
            f"  {run.source:<16} {run.state:<8} units={run.units} seen={run.pages_seen} "
            f"changed={run.pages_changed} unchanged={run.pages_unchanged} "
            f"removed={run.pages_removed} failed={run.pages_failed}"
            + (f"  ({run.detail})" if run.detail else "")
        )
    for err in status.errors[:20]:
        typer.echo(f"  ! {err.source or '-'} {err.url or ''} {err.message}", err=True)


@kb_app.command("sources")
def sources(as_json: JsonOption = False) -> None:
    """The configured sources (with admin-panel overrides) and the known components."""
    kb = _kb()
    infos = kb.source_infos(kb.stats())
    if as_json:
        _echo([i.model_dump() for i in infos])
        return
    for i in infos:
        state = "usable" if i.usable else f"not usable: {i.reason}"
        typer.echo(f"{i.id:<16} {i.kind:<9} {state}")
        typer.echo(f"    components: {', '.join(i.components)}  versions: {', '.join(i.versions)}")
        if i.url:
            typer.echo(f"    url: {i.url}")
        typer.echo(f"    pages indexed: {i.pages}")


@kb_app.command("status")
def status(as_json: JsonOption = False) -> None:
    """Index size, search mode and the last refresh."""
    st = _kb().status()
    if as_json:
        _echo(st.model_dump())
        return
    s = st.store
    typer.echo(f"store: {s.kind} {s.location}" + ("" if s.ok else f"  ERROR: {s.error}"))
    typer.echo(f"pages: {s.pages}  chunks: {s.chunks}  vectors: {s.vectors}  mode: {st.mode}")
    for comp, count in sorted(s.by_component.items()):
        typer.echo(f"  {comp:<20} {count:>6} pages  {', '.join(s.versions.get(comp, []))}")
    run = st.run
    typer.echo(
        f"last refresh: {run.last_refresh or 'never'} ({run.outcome or '-'}); state: {run.state}"
    )
    if st.next_refresh:
        typer.echo(f"next refresh: {st.next_refresh}")


@kb_app.command("refresh")
def refresh(
    rebuild: Annotated[bool, typer.Option(help="Drop and re-index the sources.")] = False,
    source: SourceOption = None,
    as_json: JsonOption = False,
) -> None:
    """Refresh the index now (incremental; one replica at a time)."""
    from autoair.kb.ingest import AlreadyRunning

    kb = _kb()
    try:
        st = kb.refresh("rebuild" if rebuild else "refresh", source)
    except AlreadyRunning as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(3) from exc
    if as_json:
        _echo(st.model_dump())
    else:
        _run_summary(st)
    if st.outcome == "failed":
        raise typer.Exit(1)


@kb_app.command("search")
def search(
    query: Annotated[str, typer.Argument(help="What to look up.")],
    component: Annotated[list[str] | None, typer.Option(help="Component filter.")] = None,
    package: Annotated[str | None, typer.Option(help="Package filter.")] = None,
    version: Annotated[str | None, typer.Option(help="Version filter.")] = None,
    limit: Annotated[int, typer.Option(min=1, max=50)] = 6,
    as_json: JsonOption = False,
) -> None:
    """Search the index (keyword + embeddings when configured)."""
    result = _kb().search(
        query, k=limit, filters={"component": component, "package": package, "version": version}
    )
    if as_json:
        _echo(result.model_dump())
        return
    typer.echo(f"mode: {result.mode}  hits: {len(result.hits)}")
    for n, hit in enumerate(result.hits, start=1):
        typer.echo(f"[{n}] {hit.title} - {hit.heading}  ({hit.package} {hit.version})")
        typer.echo(f"    {hit.url}")
        typer.echo(f"    {hit.snippet}")


@kb_app.command("build")
def build(
    out: Annotated[Path, typer.Option("--out", "-o", help="Bundle file to write (.zip).")],
    sign_key: Annotated[
        Path | None,
        typer.Option(help="Ed25519 private key (PEM) to sign the bundle (`autoair kb keygen`)."),
    ] = None,
    store_path: Annotated[
        Path | None,
        typer.Option(
            help="SQLite working index of the build (default <kb cache>/build.sqlite3; kept, "
            "so the next build is incremental)."
        ),
    ] = None,
    source: SourceOption = None,
    online: Annotated[
        bool | None,
        typer.Option(help="Override kb.online for this build (fetch the official sites)."),
    ] = None,
    description: Annotated[str, typer.Option(help="Text stored in the manifest.")] = "",
) -> None:
    """Fetch the configured sources on a connected machine and write a portable bundle
    (checksummed, optionally signed) for `autoair kb import` on an air-gapped one."""
    from autoair.kb.bundle import export_bundle
    from autoair.kb.ingest import AlreadyRunning
    from autoair.kb.stores import kb_base_dir
    from autoair.kb.stores.local import LocalStore

    settings = _settings()
    if online is not None:
        kb_cfg = settings.kb.model_copy(update={"online": online})
        settings = settings.model_copy(update={"kb": kb_cfg})
    path = store_path or kb_base_dir(settings) / "build.sqlite3"
    # Bundles carry pages, not vectors: the importer embeds with its own model.
    kb = _kb(settings, store=LocalStore(path), embedder=None)
    try:
        st = kb.refresh("refresh", source)
    except AlreadyRunning as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(3) from exc
    _run_summary(st)
    if st.outcome == "failed":
        raise typer.Exit(1)
    pem = sign_key.read_bytes() if sign_key else None
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("wb") as fh:
        manifest = export_bundle(
            kb, fh, sources=source, private_key_pem=pem, description=description
        )
    signed = "signed" if manifest.get("signature") else "unsigned"
    typer.echo(f"wrote {out} ({manifest['pages']} pages, {signed})")


@kb_app.command("import")
def import_(
    bundle: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="Bundle file.")],
    source_id: Annotated[
        str, typer.Option(help="Source id the pages are stored under (replaces its pages).")
    ] = "bundle",
    as_json: JsonOption = False,
) -> None:
    """Import a bundle into the configured store (checksums always verified, the signature
    when kb.bundle.public_keys is set)."""
    from autoair.kb.bundle import BundleError, import_bundle
    from autoair.kb.ingest import AlreadyRunning

    try:
        report, _ = import_bundle(_kb(), bundle, source_id=source_id)
    except (BundleError, AlreadyRunning) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    if as_json:
        _echo(report.model_dump())
        return
    typer.echo(
        f"imported {report.pages} pages ({report.changed} changed, {report.unchanged} "
        f"unchanged, {report.removed} removed, {report.chunks} chunks); signature: "
        + ("verified" if report.signature_verified else "signed" if report.signed else "none")
    )


@kb_app.command("keygen")
def keygen(
    private_key: Annotated[
        Path, typer.Option("--out", help="Private key file to write (PEM, mode 0600).")
    ] = Path("kb-bundle.pem"),
) -> None:
    """Create a bundle signing key; prints the public key for kb.bundle.public_keys."""
    import os

    from autoair.marketplace import generate_keypair

    pem, public = generate_keypair()
    fd = os.open(private_key, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(pem)
    typer.echo(f"private key: {private_key}")
    typer.echo(f"public key (AUTOAIR_KB__BUNDLE__PUBLIC_KEYS): {public}")


@kb_app.command("mcp")
def mcp() -> None:
    """Serve only the knowledge-base tools over MCP (stdio), for IDE agents."""
    try:
        from autoair.kb.mcp import create_kb_mcp_server
    except ImportError as exc:  # pragma: no cover - needs autoair[mcp]
        typer.echo(f"MCP needs autoair[mcp]: {exc}", err=True)
        raise typer.Exit(2) from exc
    settings = _settings()
    if settings.feature_overrides is None:
        # Runtime feature switches of the admin panel apply to this server too.
        from autoair.feature_overrides import override_store_for

        settings.attach_feature_overrides(override_store_for(settings))

    class _Services:
        def __init__(self) -> None:
            self.settings = settings
            self.llm = None

    services = _Services()
    create_kb_mcp_server(lambda: services).run("stdio", show_banner=False)


__all__ = ["kb_app"]
