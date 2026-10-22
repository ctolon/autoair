"""Constraints for testing AutoAir next to one Airflow release, derived from Airflow itself.

Airflow's constraints file pins every package to what that release was tested with, but it
cannot be applied whole: it also pins packages below AutoAir's own requirements
(website/docs/installation/compatibility.md, "Airflow constraints files"). A fresh resolve
without it picks whatever was released last, and an upstream release can break Airflow
itself (FastAPI 0.156.1 broke Airflow 3.2's execution API). This script keeps the part that
protects Airflow: the pins of the packages ``apache-airflow-core`` and
``apache-airflow-task-sdk`` depend on directly. Pins that conflict with AutoAir's
requirements are dropped automatically and reported, so a new Airflow or AutoAir release
needs no hand-written pin.

Stdlib only: CI runs it before the test environment exists. Needs ``uv`` on PATH.

    python scripts/airflow_constraints.py --airflow 3.2.2 --python 3.12 \\
        --output constraints.txt -- -e ".[all]" apache-airflow==3.2.2 pytest
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from pathlib import Path

CONSTRAINTS_URL = "https://raw.githubusercontent.com/apache/airflow/constraints-{airflow}/constraints-{python}.txt"
PYPI_URL = "https://pypi.org/pypi/{name}/{version}/json"
# The distributions whose direct dependencies Airflow's runtime is written against.
CORE_DISTRIBUTIONS = ("apache-airflow-core", "apache-airflow-task-sdk")
# Python minors Airflow 3 publishes constraints for; used only to find the nearest file when
# the requested Python has none (a new Python before Airflow publishes for it).
KNOWN_PYTHONS = ("3.9", "3.10", "3.11", "3.12", "3.13", "3.14", "3.15")
RETRIES = 3
TIMEOUT = 30

# pins -> (resolved, the annotated resolution on success or the resolver's error)
Resolver = Callable[[dict[str, str]], tuple[bool, str]]


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_constraints(text: str) -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*==\s*([^\s;#]+)", line)
        if match:
            pins[canonical(match[1])] = match[2]
    return pins


def direct_dependencies(requires_dist: Iterable[str]) -> set[str]:
    """Names of the unconditional (non-extra) requirements in wheel metadata."""
    names = set()
    for requirement in requires_dist:
        _, _, marker = requirement.partition(";")
        if re.search(r"\bextra\s*==", marker):
            continue
        match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", requirement)
        if match:
            names.add(canonical(match[1]))
    return names


def core_pins(pins: dict[str, str], dependencies: set[str]) -> dict[str, str]:
    return {
        name: pins[name]
        for name in sorted(dependencies)
        if name in pins and not name.startswith("apache-airflow")
    }


def named_in_conflict(error: str, candidates: dict[str, str]) -> str | None:
    """The first pin the resolver names in its error, e.g. ``libcst==1.8.2``."""
    text = " ".join(error.lower().split())
    for name, version in candidates.items():
        spellings = {name, name.replace("-", "_")}
        for spelling in spellings:
            if re.search(
                rf"(?<![\w.-]){re.escape(spelling)}=={re.escape(version.lower())}(?![\w.])", text
            ):
                return name
    return None


def required_by(resolved: str) -> dict[str, set[str]]:
    """From ``uv pip compile`` output: package -> the packages that require it (``# via``)."""
    graph: dict[str, set[str]] = {}
    current: str | None = None
    in_via = False
    for line in resolved.splitlines():
        match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?\s*==", line)
        if match:
            current, in_via = canonical(match[1]), False
            graph.setdefault(current, set())
            continue
        comment = line.strip()
        if current is None or not comment.startswith("#"):
            continue
        body = comment.lstrip("#").strip()
        if body.startswith("via"):
            in_via, body = True, body[3:].strip()
        if in_via and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", body):
            graph[current].add(canonical(body))
    return graph


def reduce_pins(
    pins: dict[str, str], resolve: Resolver
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """Drop pins until the requirements resolve.

    Returns (kept, dropped, dropped_with): ``dropped`` conflict with the requirements;
    ``dropped_with`` depend on a dropped package. Keeping a pin whose dependency moved to a
    newer release would produce a combination nobody tested (cadwyn 5.4 pinned next to a
    FastAPI it cannot import), which is worse than not pinning it."""
    ok, error = resolve({})
    if not ok:
        raise SystemExit(f"the requirements do not resolve even without constraints:\n{error}")
    kept = dict(pins)
    dropped: dict[str, str] = {}
    # Fast path: the resolver usually names the conflicting pin.
    for _ in range(len(pins) + 1):
        ok, output = resolve(kept)
        if ok:
            break
        name = named_in_conflict(output, kept)
        if name is None:
            break
        dropped[name] = kept.pop(name)
    if not ok:
        # Fallback that does not depend on the resolver's wording: add pins one at a time.
        kept, dropped = {}, {}
        for name, version in pins.items():
            ok, attempt = resolve({**kept, name: version})
            if ok:
                kept[name], output = version, attempt
            else:
                dropped[name] = version
        if not kept:
            ok, output = resolve({})
    dropped_with: dict[str, str] = {}
    while True:
        graph = required_by(output)
        loose = set(dropped) | set(dropped_with)
        dependents = {r for name in loose for r in graph.get(name, ()) if r in kept}
        if not dependents:
            return kept, dropped, dropped_with
        for name in sorted(dependents):
            dropped_with[name] = kept.pop(name)
        ok, output = resolve(kept)
        if not ok:  # fewer pins than a resolvable set cannot conflict; keep it explicit
            raise SystemExit(f"dropping dependent pins made the set unresolvable:\n{output}")


def fetch(url: str) -> str | None:
    """The body, or None on 404; other errors are retried, then fatal."""
    if not url.startswith("https://"):
        raise SystemExit(f"refusing to fetch {url}: https only")
    for attempt in range(1, RETRIES + 1):
        try:
            with urllib.request.urlopen(url, timeout=TIMEOUT) as response:  # noqa: S310
                return str(response.read().decode())
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            failure: Exception = exc
        except (urllib.error.URLError, TimeoutError) as exc:
            failure = exc
        if attempt == RETRIES:
            raise SystemExit(f"cannot fetch {url}: {failure}") from failure
        time.sleep(2**attempt)
    return None


def nearest_pythons(python: str) -> list[str]:
    def distance(candidate: str) -> tuple[int, int]:
        minor, wanted = int(candidate.split(".")[1]), int(python.split(".")[1])
        return abs(minor - wanted), -minor

    return [python, *sorted((p for p in KNOWN_PYTHONS if p != python), key=distance)]


def airflow_constraints(airflow: str, python: str) -> tuple[str, dict[str, str]] | None:
    for candidate in nearest_pythons(python):
        url = CONSTRAINTS_URL.format(airflow=airflow, python=candidate)
        text = fetch(url)
        if text is not None:
            return url, parse_constraints(text)
    return None


def requires_dist(name: str, version: str) -> list[str]:
    body = fetch(PYPI_URL.format(name=name, version=version))
    if body is None:
        raise SystemExit(f"{name}=={version} is not on PyPI")
    return list(json.loads(body)["info"]["requires_dist"] or [])


def uv_resolver(requirements: list[str], python: str, prerelease: bool, work: Path) -> Resolver:
    reqs = work / "requirements.in"
    reqs.write_text("\n".join(requirements) + "\n")

    def resolve(pins: dict[str, str]) -> tuple[bool, str]:
        constraints = work / "constraints.txt"
        constraints.write_text("".join(f"{n}=={v}\n" for n, v in pins.items()))
        command = [
            "uv", "pip", "compile", "--quiet", "--no-header", "--python-version", python,
            "--output-file", str(work / "resolved.txt"), str(reqs),
            "--constraint", str(constraints),
        ]  # fmt: skip
        if prerelease:
            command.append("--prerelease=allow")
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode == 0:
            return True, (work / "resolved.txt").read_text()
        return False, result.stderr + result.stdout

    return resolve


def report(lines: list[str]) -> None:
    print("\n".join(lines), file=sys.stderr)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as handle:
            handle.write("\n".join(lines) + "\n\n")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--airflow", required=True, help="Airflow version, e.g. 3.2.2")
    parser.add_argument("--python", required=True, help="Python minor, e.g. 3.12")
    parser.add_argument("--output", required=True, type=Path, help="constraints file to write")
    parser.add_argument("--prerelease", action="store_true", help="allow pre-releases")
    parser.add_argument("requirements", nargs="+", help="what will be installed (after --)")
    args = parser.parse_args(argv)
    python = ".".join(args.python.split(".")[:2])
    requirements: list[str] = []
    for item in args.requirements:  # `-e .[x]` arrives as two arguments
        if requirements and requirements[-1] == "-e":
            requirements[-1] = f"-e {item}"
        else:
            requirements.append(item)

    title = f"### Airflow {args.airflow} constraints (Python {python})"
    found = airflow_constraints(args.airflow, python)
    if found is None:
        args.output.write_text(f"# Airflow {args.airflow} publishes no constraints file\n")
        report([title, f"WARNING: no constraints file for Airflow {args.airflow}; unpinned."])
        return
    url, pins = found
    dependencies: set[str] = set()
    for name in CORE_DISTRIBUTIONS:
        version = args.airflow if name == "apache-airflow-core" else pins.get(name)
        if version:
            dependencies |= direct_dependencies(requires_dist(name, version))
    candidates = core_pins(pins, dependencies)
    with tempfile.TemporaryDirectory(prefix="airflow-constraints-") as work:
        resolve = uv_resolver(requirements, python, args.prerelease, Path(work))
        kept, dropped, dropped_with = reduce_pins(candidates, resolve)
    args.output.write_text(
        f"# Derived from {url}\n# by scripts/airflow_constraints.py; do not edit.\n"
        + "".join(f"{n}=={v}\n" for n, v in kept.items())
    )
    lines = [title, f"Source: {url}", f"Kept {len(kept)} of {len(candidates)} core pins."]
    if dropped:
        lines.append("Dropped (conflict with AutoAir's requirements): " + ", ".join(
            f"`{n}=={v}`" for n, v in dropped.items()
        ))  # fmt: skip
    if dropped_with:
        lines.append("Dropped (depend on a dropped package): " + ", ".join(
            f"`{n}=={v}`" for n, v in dropped_with.items()
        ))  # fmt: skip
    report(lines)


if __name__ == "__main__":
    main()
