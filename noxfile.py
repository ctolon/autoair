"""Airflow compatibility matrix: ``uv run nox`` (or ``nox -s tests-3.0``)."""

from __future__ import annotations

import os

import nox

nox.options.default_venv_backend = "uv"
nox.options.reuse_existing_virtualenvs = True

# Latest patch release of every supported Airflow 3 minor.
AIRFLOW_VERSIONS = ["3.0.6", "3.1.8", "3.2.2", "3.3.2"]
# Pre-releases are tested but allowed to fail in CI until they are final.
AIRFLOW_PRERELEASES = ["3.4.0b1"]


@nox.session(python="3.12")
@nox.parametrize("airflow", AIRFLOW_VERSIONS, ids=[v.rsplit(".", 1)[0] for v in AIRFLOW_VERSIONS])
def tests(session: nox.Session, airflow: str) -> None:
    session.install(
        "-e",
        ".[all]",
        "apache-airflow-providers-git",
        "apache-airflow-providers-common-messaging",
        f"apache-airflow=={airflow}",
        "apache-airflow-providers-standard",
        "pytest",
        "ruff",
    )
    home = os.path.abspath(session.create_tmp())
    session.run(
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        *session.posargs,
        env={"AIRFLOW_HOME": home, "AIRFLOW__CORE__LOAD_EXAMPLES": "False"},
    )


@nox.session(python="3.12")
@nox.parametrize("airflow", AIRFLOW_PRERELEASES, ids=AIRFLOW_PRERELEASES)
def prerelease(session: nox.Session, airflow: str) -> None:
    session.install(
        "--prerelease=allow",
        "-e",
        ".[all]",
        "apache-airflow-providers-git",
        "apache-airflow-providers-common-messaging",
        f"apache-airflow=={airflow}",
        "apache-airflow-providers-standard",
        "pytest",
        "ruff",
    )
    home = os.path.abspath(session.create_tmp())
    session.run(
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        *session.posargs,
        env={"AIRFLOW_HOME": home, "AIRFLOW__CORE__LOAD_EXAMPLES": "False"},
    )


@nox.session(python="3.10")
def oldest(session: nox.Session) -> None:
    """Lowest supported Python with the oldest supported Airflow."""
    session.install(
        "-e",
        ".[all]",
        "apache-airflow-providers-git",
        "apache-airflow-providers-common-messaging",
        f"apache-airflow=={AIRFLOW_VERSIONS[0]}",
        "apache-airflow-providers-standard",
        "pytest",
        "ruff",
    )
    home = os.path.abspath(session.create_tmp())
    session.run(
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        *session.posargs,
        env={"AIRFLOW_HOME": home, "AIRFLOW__CORE__LOAD_EXAMPLES": "False"},
    )


@nox.session(python="3.12")
def lint(session: nox.Session) -> None:
    session.install(
        "-e",
        ".[all]",
        f"apache-airflow=={AIRFLOW_VERSIONS[-1]}",
        "apache-airflow-providers-standard",
        "apache-airflow-providers-git",
        "apache-airflow-providers-common-messaging",
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
