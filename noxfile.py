"""Airflow compatibility matrix: ``uv run nox`` (or ``nox -s tests-3.0``)."""

from __future__ import annotations

import os
import tempfile

import nox

nox.options.default_venv_backend = "uv"
nox.options.reuse_existing_virtualenvs = True

# Latest patch release of every supported Airflow 3 minor.
AIRFLOW_VERSIONS = ["3.0.6", "3.1.8", "3.2.2", "3.3.2"]
# Pre-releases are tested but allowed to fail in CI until they are final.
AIRFLOW_PRERELEASES = ["3.4.0b2"]

# Extras installed next to each Airflow: everything that resolves there, so the suite runs
# what users can install and skips only what is unavailable. Keep in sync with the `extras`
# of the test matrix in .github/workflows/ci.yml and with
# website/docs/installation/compatibility.md.
EXTRAS = "all,mcp,blueprint,sqlite,postgres,kb-pgvector"
# autoair[mcp] (FastMCP 4) needs Starlette >= 1.0.1; Airflow 3.1 pins FastAPI < 0.118, i.e.
# Starlette < 0.49. Its MCP tests skip with that reason.
EXTRAS_BY_MINOR = {"3.1": "all,blueprint,sqlite,postgres,kb-pgvector"}

PROVIDERS = (
    "apache-airflow-providers-standard",
    "apache-airflow-providers-git",
    "apache-airflow-providers-common-messaging",
)


def extras_for(airflow: str) -> str:
    return EXTRAS_BY_MINOR.get(".".join(airflow.split(".")[:2]), EXTRAS)


def _install(
    session: nox.Session, airflow: str, *extra_args: str, constrained: bool = True
) -> None:
    """Install AutoAir next to ``airflow``; ``constrained`` applies the pins Airflow's own
    constraints file sets for its core dependencies (scripts/airflow_constraints.py), as the
    blocking CI matrix does. Unconstrained is a fresh resolve, as `pip install` does."""
    requirements = [
        "-e",
        f".[{extras_for(airflow)}]",
        f"apache-airflow=={airflow}",
        *PROVIDERS,
        "pytest",
        "ruff",
    ]
    constraints: list[str] = []
    if constrained:
        # Not session.create_tmp(), which would also move pytest's TMPDIR (see _pytest).
        folder = tempfile.mkdtemp(prefix="autoair-nox-constraints-")
        path = os.path.join(folder, f"airflow-{airflow}-constraints.txt")
        flags = ["--prerelease"] if "--prerelease=allow" in extra_args else []
        session.run(
            "python",
            "scripts/airflow_constraints.py",
            *("--airflow", airflow, "--python", str(session.python), "--output", path),
            *flags,
            "--",
            *requirements,
        )
        constraints = ["--constraint", path]
    session.install(*extra_args, *requirements, *constraints)


def _pytest(session: nox.Session) -> None:
    # Not session.create_tmp(): it points TMPDIR into the virtualenv, so pytest's tmp_path
    # would sit below the Python installation (which the LSP file peek may read).
    home = tempfile.mkdtemp(prefix="autoair-nox-airflow-")
    session.run(
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        *session.posargs,
        env={"AIRFLOW_HOME": home, "AIRFLOW__CORE__LOAD_EXAMPLES": "False"},
    )


@nox.session(python="3.12")
@nox.parametrize("airflow", AIRFLOW_VERSIONS, ids=[v.rsplit(".", 1)[0] for v in AIRFLOW_VERSIONS])
def tests(session: nox.Session, airflow: str) -> None:
    _install(session, airflow)
    _pytest(session)


@nox.session(python="3.12")
@nox.parametrize("airflow", AIRFLOW_VERSIONS, ids=[v.rsplit(".", 1)[0] for v in AIRFLOW_VERSIONS])
def fresh(session: nox.Session, airflow: str) -> None:
    """Without Airflow's constraints: the newest releases a plain `pip install` picks, which
    shows an upstream release breaking Airflow or AutoAir before users report it."""
    _install(session, airflow, constrained=False)
    _pytest(session)


@nox.session(python="3.12")
@nox.parametrize("airflow", AIRFLOW_PRERELEASES, ids=AIRFLOW_PRERELEASES)
def prerelease(session: nox.Session, airflow: str) -> None:
    _install(session, airflow, "--prerelease=allow")
    _pytest(session)


@nox.session(python="3.10")
def oldest(session: nox.Session) -> None:
    """Lowest supported Python with the oldest supported Airflow."""
    _install(session, AIRFLOW_VERSIONS[0])
    _pytest(session)


@nox.session(python="3.12")
def lint(session: nox.Session) -> None:
    session.install(
        "-e",
        f".[{extras_for(AIRFLOW_VERSIONS[-1])}]",
        f"apache-airflow=={AIRFLOW_VERSIONS[-1]}",
        *PROVIDERS,
        "ruff",
        "mypy",
        "types-pyyaml",
    )
    session.run("ruff", "check", "src", "tests")
    session.run("ruff", "format", "--check", "src", "tests")
    session.run("mypy")


@nox.session(python="3.12")
def noai(session: nox.Session) -> None:
    """Without the ``ai`` extra (no pydantic-ai): the app, the MCP server and the CLI work
    with AI off."""
    session.install("-e", ".[standalone,mcp]", "pytest")
    session.run(
        "python", "-c", "import importlib.util as u; assert u.find_spec('pydantic_ai') is None"
    )
    home = os.path.abspath(session.create_tmp())
    session.run(
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        *(
            session.posargs
            or ["tests/integration/test_ai_unavailable.py", "tests/integration/test_kb_noai.py"]
        ),
        env={"AIRFLOW_HOME": home, "AIRFLOW__CORE__LOAD_EXAMPLES": "False"},
    )
