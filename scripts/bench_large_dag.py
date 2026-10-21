"""Backend timings for large DAGs: spec parsing, validation, code generation, export, import.

Uses the synthetic DAGs of ``gen_large_dag.py`` (or your own ``--spec`` files) and prints the
median of ``--repeat`` runs per step as a Markdown table, so numbers can be pasted into a
pull request or compared before/after a change.

    uv run python scripts/bench_large_dag.py                       # 100/500/1000 tasks
    uv run python scripts/bench_large_dag.py --tasks 500 --repeat 5
    uv run python scripts/bench_large_dag.py --import-check        # also DagBag + importer
    uv run python scripts/bench_large_dag.py --spec my.autoair.json --catalog

``--catalog`` validates operator params against the operator catalog, as the API does (the
first use builds or loads the catalog; that time is not counted). ``--import-check`` adds the
DagBag import of the generated file and the Python importer (generated file -> spec); both
start Airflow in a subprocess and take seconds.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import statistics
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _generator() -> Any:
    spec = importlib.util.spec_from_file_location(
        "gen_large_dag", ROOT / "scripts" / "gen_large_dag.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _median(fn: Callable[[], Any], repeat: int) -> float:
    times = []
    for _ in range(repeat):
        start = time.perf_counter()
        fn()
        times.append(time.perf_counter() - start)
    return statistics.median(times)


def bench(
    data: dict[str, Any], *, repeat: int, catalog: Any = None, import_check: bool = False
) -> dict[str, float]:
    from autoair.codegen import render
    from autoair.codegen.dagfactory import export_dag_factory
    from autoair.codegen.yaml_dag import export_yaml_dag
    from autoair.spec import DagSpec
    from autoair.validation import validate_spec

    spec = DagSpec.model_validate(data)
    out: dict[str, float] = {}
    out["parse"] = _median(lambda: DagSpec.model_validate(data), repeat)

    def cold_validate() -> None:
        # The first validation of a freshly opened DAG: no memoised lint results.
        for module in ("autoair.validation.pylint", "autoair.validation.shell"):
            clear = getattr(importlib.import_module(module), "clear_cache", None)
            if clear is not None:
                clear()
        validate_spec(spec, catalog=catalog)

    out["validate (cold)"] = _median(cold_validate, repeat)
    out["validate (warm)"] = _median(lambda: validate_spec(spec, catalog=catalog), repeat)
    out["codegen"] = _median(lambda: render(spec), repeat)
    out["codegen (unformatted)"] = _median(lambda: render(spec, fmt=False), repeat)
    out["yaml export"] = _median(lambda: export_yaml_dag(spec), repeat)
    out["dag-factory export"] = _median(lambda: export_dag_factory(spec), repeat)
    out["spec json"] = _median(lambda: spec.model_dump_json(exclude_none=True), repeat)
    if import_check:
        from autoair.importer.python_dag import import_python_source
        from autoair.validation.dagbag_check import check_source

        source = render(spec)
        out["dagbag import"] = _median(
            lambda: check_source(source, filename=f"{spec.dag_id}.py", timeout=600), 1
        )
        out["import python"] = _median(
            lambda: import_python_source(source, filename=f"{spec.dag_id}.py", timeout=600), 1
        )
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--tasks", type=int, action="append", help="task counts (default 100 500 1000)")
    ap.add_argument("--spec", type=Path, action="append", help="benchmark this spec file instead")
    ap.add_argument("--repeat", type=int, default=3, help="runs per step (median is reported)")
    ap.add_argument("--catalog", action="store_true", help="validate against the operator catalog")
    ap.add_argument("--import-check", action="store_true", help="also DagBag import and importer")
    ap.add_argument("--json", action="store_true", help="print JSON instead of a Markdown table")
    args = ap.parse_args(argv)

    inputs: list[tuple[str, dict[str, Any]]] = []
    if args.spec:
        inputs = [(p.name, json.loads(p.read_text(encoding="utf-8"))) for p in args.spec]
    else:
        gen = _generator()
        for n in args.tasks or [100, 500, 1000]:
            inputs.append((f"{n} tasks", gen.large_dag_spec(n)))

    catalog = None
    if args.catalog:
        from autoair.catalog import OperatorCatalog

        catalog = OperatorCatalog()
        catalog.warm()  # build/load outside the timings

    # Warm-up: imports, ruff and Jinja caches are not part of a request's cost in a server.
    bench(inputs[0][1], repeat=1, catalog=catalog)

    results = {
        name: bench(data, repeat=args.repeat, catalog=catalog, import_check=args.import_check)
        for name, data in inputs
    }
    if args.json:
        print(json.dumps(results, indent=1))
        return 0
    steps = list(next(iter(results.values())))
    print("| step | " + " | ".join(results) + " |")
    print("|---|" + "---:|" * len(results))
    for step in steps:
        cells = " | ".join(f"{results[name][step] * 1000:.0f} ms" for name in results)
        print(f"| {step} | {cells} |")
    nodes = " | ".join(f"{len(d['nodes'])} nodes / {len(d['edges'])} edges" for _, d in inputs)
    print(f"| size | {nodes} |", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
