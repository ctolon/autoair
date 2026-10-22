"""scripts/airflow_constraints.py: the CI matrix's pins are derived from Airflow's constraints
file, so a new Airflow or AutoAir release needs no hand-written pin."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import airflow_constraints as ac

CONSTRAINTS = """\
#
# This constraints file was automatically generated on 2026-05-01
Flask_Session==0.8.0
fastapi==0.136.1
libcst==1.8.2
pydantic==2.13.4
apache-airflow-task-sdk==1.2.2
requests==2.32.5
"""


def test_parse_constraints_normalises_names() -> None:
    pins = ac.parse_constraints(CONSTRAINTS)
    assert pins["flask-session"] == "0.8.0"
    assert pins["fastapi"] == "0.136.1"
    assert "#" not in "".join(pins)


def test_only_unconditional_requirements_count() -> None:
    names = ac.direct_dependencies(
        [
            "fastapi[standard-no-fastapi-cloud-cli]>=0.129",
            "pydantic>=2.11.0",
            'libcst>=1.8.2; python_version >= "3.10"',
            'boto3>=1.0; extra == "amazon"',
            "Flask_Session",
        ]
    )
    assert names == {"fastapi", "pydantic", "libcst", "flask-session"}


def test_core_pins_skip_airflow_distributions_and_unpinned_names() -> None:
    pins = ac.parse_constraints(CONSTRAINTS)
    selected = ac.core_pins(pins, {"fastapi", "apache-airflow-task-sdk", "not-pinned"})
    assert selected == {"fastapi": "0.136.1"}


def test_conflict_detection_matches_whole_versions_only() -> None:
    error = (
        "Because autoair==0.2.3 depends on libcst>=1.8.6 and libcst==1.8.2,\n"
        "  we can conclude that autoair==0.2.3 cannot be used."
    )
    assert ac.named_in_conflict(error, {"fastapi": "0.136.1", "libcst": "1.8.2"}) == "libcst"
    assert ac.named_in_conflict("fastapi==0.136.10 is required", {"fastapi": "0.136.1"}) is None
    assert ac.named_in_conflict("Flask_Session==0.8.0", {"flask-session": "0.8.0"}) == (
        "flask-session"
    )


RESOLVED = """\
cadwyn==6.0.0
    # via apache-airflow-core
fastapi[standard]==0.159.1
    # via
    #   apache-airflow-core
    #   cadwyn
libcst==1.8.6
    # via autoair
opentelemetry-api==1.30.0
    # via opentelemetry-sdk
opentelemetry-sdk==1.30.0
    # via autoair
"""


def test_required_by_reads_uv_annotations() -> None:
    graph = ac.required_by(RESOLVED)
    assert graph["fastapi"] == {"apache-airflow-core", "cadwyn"}
    assert graph["cadwyn"] == {"apache-airflow-core"}
    assert graph["opentelemetry-api"] == {"opentelemetry-sdk"}


def _resolver(conflicts: set[str], wording: bool):
    calls: list[dict[str, str]] = []

    def resolve(pins: dict[str, str]) -> tuple[bool, str]:
        calls.append(dict(pins))
        bad = sorted(set(pins) & conflicts)
        if not bad:
            return True, RESOLVED
        return False, (f"depends on {bad[0]}=={pins[bad[0]]}" if wording else "no solution")

    return resolve, calls


@pytest.mark.parametrize("wording", [True, False], ids=["named", "unparseable-error"])
def test_conflicting_pins_are_dropped_and_the_rest_kept(wording: bool) -> None:
    pins = {"requests": "2.32.5", "libcst": "1.8.2", "opentelemetry-api": "1.27.0"}
    resolve, calls = _resolver({"libcst", "opentelemetry-api"}, wording)
    kept, dropped, dropped_with = ac.reduce_pins(pins, resolve)
    assert kept == {"requests": "2.32.5"}
    assert dropped == {"libcst": "1.8.2", "opentelemetry-api": "1.27.0"}
    assert dropped_with == {}
    assert calls[0] == {}  # first checks that the requirements resolve at all


def test_pins_that_depend_on_a_dropped_package_go_too() -> None:
    """Airflow 3.0: fastapi is unpinned for autoair[mcp]; cadwyn 5.4 pinned next to the newer
    FastAPI failed to import, so every test run failed."""
    pins = {"cadwyn": "5.4.4", "fastapi": "0.116.1", "requests": "2.32.5"}
    resolve, _ = _resolver({"fastapi"}, wording=True)
    kept, dropped, dropped_with = ac.reduce_pins(pins, resolve)
    assert dropped == {"fastapi": "0.116.1"}
    assert dropped_with == {"cadwyn": "5.4.4"}
    assert kept == {"requests": "2.32.5"}


def test_unresolvable_requirements_fail_loudly() -> None:
    with pytest.raises(SystemExit, match="do not resolve even without constraints"):
        ac.reduce_pins({"fastapi": "1"}, lambda pins: (False, "boom"))


def test_a_missing_python_falls_back_to_the_nearest_published_file() -> None:
    assert ac.nearest_pythons("3.14")[:3] == ["3.14", "3.15", "3.13"]
    assert ac.nearest_pythons("3.10")[1] == "3.11"


def test_only_https_is_fetched() -> None:
    with pytest.raises(SystemExit, match="https only"):
        ac.fetch("file:///etc/passwd")
