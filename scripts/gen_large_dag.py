"""Generate large, realistic DagSpecs for performance work (benchmarks, E2E, screenshots).

The DAG is a chain of task groups ("stages"). Each stage holds a small tree of tasks plus a
few extra cross edges, so the graph has roughly 1.4 edges per task. The task mix follows what
real pipelines look like: Bash and Python tasks, empty join points, sensors, mapped tasks
(over a literal list and over an upstream XCom), setup/teardown pairs, labelled edges and a
few sticky notes. The output is deterministic for a given ``--seed``.

    uv run python scripts/gen_large_dag.py --tasks 500 > big.autoair.json
    uv run python scripts/gen_large_dag.py --tasks 1000 --no-layout -o big.json

``--no-layout`` leaves every position at 0/0 (to measure ELK auto-layout on load); the
default places the nodes on a readable grid, as a laid-out document would be stored.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path
from typing import Any

BASH = "airflow.providers.standard.operators.bash.BashOperator"
EMPTY = "airflow.providers.standard.operators.empty.EmptyOperator"
TIME_DELTA_SENSOR = "airflow.providers.standard.sensors.time_delta.TimeDeltaSensor"
TRIGGER = "airflow.providers.standard.operators.trigger_dagrun.TriggerDagRunOperator"

# Canvas geometry of the default layout (task cards are ~260x70 on the canvas).
COL_W = 300
ROW_H = 110
GROUP_PAD_X = 30
GROUP_PAD_Y = 60
GROUP_GAP = 80
GROUPS_PER_ROW = 4


def _task(kind: str, i: int, rnd: random.Random) -> dict[str, Any]:
    tid = f"t{i:04d}"
    if kind == "bash":
        node: dict[str, Any] = {
            "kind": "operator",
            "operator": BASH,
            "task_id": f"bash_{tid}",
            "params": {"bash_command": f"echo step {i} {{{{ ds }}}}"},
        }
        if rnd.random() < 0.2:
            node["retries"] = 3
            node["execution_timeout_seconds"] = 600
        return node
    if kind == "empty":
        return {"kind": "operator", "operator": EMPTY, "task_id": f"join_{tid}"}
    if kind == "sensor":
        return {
            "kind": "operator",
            "operator": TIME_DELTA_SENSOR,
            "task_id": f"wait_{tid}",
            "params": {"delta": 60, "mode": "reschedule"},
        }
    if kind == "trigger":
        return {
            "kind": "operator",
            "operator": TRIGGER,
            "task_id": f"trigger_{tid}",
            "params": {"trigger_dag_id": "downstream_dag"},
        }
    if kind == "mapped_bash":
        return {
            "kind": "operator",
            "operator": BASH,
            "task_id": f"fan_{tid}",
            "params": {},
            "mapping": {"expand": {"bash_command": [f"echo {k}" for k in range(3)]}},
        }
    # python
    return {
        "kind": "python_task",
        "task_id": f"py_{tid}",
        "function_name": f"py_{tid}",
        "arguments": {},
        "code": f"rows = [{i}, {i + 1}, {i + 2}]\nreturn rows",
    }


KINDS = ["bash"] * 35 + ["python"] * 30 + ["empty"] * 12 + ["sensor"] * 8
KINDS += ["mapped_bash"] * 5 + ["trigger"] * 2


def large_dag_spec(
    tasks: int = 500,
    *,
    group_size: int = 12,
    seed: int = 0,
    layout: bool = True,
    notes: int = 3,
    dag_id: str | None = None,
) -> dict[str, Any]:
    """A DagSpec (as JSON data) with ``tasks`` task nodes in stages of ``group_size``."""
    rnd = random.Random(seed)  # noqa: S311 - reproducible test data, not security
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    stages = max(1, -(-tasks // group_size))
    i = 0
    prev_group: str | None = None
    prev_last: str | None = None
    cursor_x = cursor_y = 0.0
    row_h = 0.0
    for g in range(stages):
        gid = f"g{g:03d}"
        count = min(group_size, tasks - i)
        if count <= 0:
            break
        members: list[dict[str, Any]] = []
        depth: dict[str, int] = {}
        python_members: list[str] = []
        for k in range(count):
            kind = rnd.choice(KINDS) if k else "empty"
            node = _task(kind, i, rnd)
            node["id"] = f"n{i:04d}"
            node["parent_group"] = gid
            i += 1
            if k:
                parent = members[rnd.randrange(max(0, k - 4), k)]
                edges.append({"source": parent["id"], "target": node["id"]})
                depth[node["id"]] = depth[parent["id"]] + 1
                if rnd.random() < 0.75 and k > 1:
                    other = members[rnd.randrange(0, k)]
                    if other is not parent and depth[other["id"]] <= depth[parent["id"]]:
                        edges.append({"source": other["id"], "target": node["id"]})
                # Mapped over an upstream Python task's XCom (it returns a list).
                if node["kind"] == "python_task" and python_members and rnd.random() < 0.15:
                    src = python_members[-1]
                    node["arguments"] = {}
                    node["extra_parameters"] = []
                    node["mapping"] = {"expand": {"row": {"$type": "xcom", "node": src}}}
                    node["code"] = "return row"
                    if depth[node["id"]] <= depth[src]:
                        depth[node["id"]] = depth[src] + 1
            else:
                depth[node["id"]] = 0
            if node["kind"] == "python_task" and "mapping" not in node:
                python_members.append(node["id"])
            members.append(node)
        # One setup/teardown pair per few stages.
        if g % 5 == 2 and count >= 4:
            members[1]["is_setup"] = True
            members[-1]["is_teardown"] = True
            pair = {"source": members[1]["id"], "target": members[-1]["id"]}
            if depth[members[1]["id"]] < depth[members[-1]["id"]] and pair not in edges:
                edges.append(pair)
        # Group size from the member grid.
        cols: dict[int, int] = {}
        for m in members:
            d = depth[m["id"]]
            row = cols.get(d, 0)
            cols[d] = row + 1
            if layout:
                m["position"] = {"x": GROUP_PAD_X + d * COL_W, "y": GROUP_PAD_Y + row * ROW_H}
        width = GROUP_PAD_X * 2 + (max(cols) + 1) * COL_W
        height = GROUP_PAD_Y + max(cols.values()) * ROW_H + GROUP_PAD_X
        group: dict[str, Any] = {"kind": "task_group", "id": gid, "group_id": f"stage_{g:03d}"}
        if layout:
            if g and g % GROUPS_PER_ROW == 0:
                cursor_x = 0.0
                cursor_y += row_h + GROUP_GAP
                row_h = 0.0
            group["position"] = {"x": cursor_x, "y": cursor_y, "width": width, "height": height}
            cursor_x += width + GROUP_GAP
            row_h = max(row_h, height)
        nodes.append(group)
        nodes.extend(members)
        # Stages run in sequence: mostly group -> group, sometimes task -> task across stages.
        if prev_group is not None and prev_last is not None:
            if rnd.random() < 0.7:
                edge: dict[str, Any] = {"source": prev_group, "target": gid}
                if rnd.random() < 0.3:
                    edge["label"] = f"stage {g} ready"
                edges.append(edge)
            else:
                edges.append({"source": prev_last, "target": members[0]["id"]})
        prev_group = gid
        prev_last = members[-1]["id"]
    for k in range(notes):
        note: dict[str, Any] = {
            "kind": "note",
            "id": f"note{k}",
            "text": f"**Stage notes {k}**\n\nGenerated by gen_large_dag.py",
        }
        if layout:
            note["position"] = {"x": -400, "y": k * 260, "width": 260, "height": 200}
        nodes.append(note)
    return {
        "spec_version": 3,
        "dag_id": dag_id or f"large_dag_{tasks}",
        "description": f"Synthetic {tasks}-task DAG for performance tests",
        "schedule": {"type": "cron", "expression": "0 3 * * *"},
        "start_date": "2026-01-01T00:00:00Z",
        "catchup": False,
        "tags": ["autoair", "perf"],
        "default_args": {"owner": "perf", "retries": 1, "retry_delay_seconds": 300},
        "nodes": nodes,
        "edges": edges,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--tasks", type=int, default=500, help="number of task nodes (default 500)")
    ap.add_argument("--group-size", type=int, default=12, help="tasks per task group")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--notes", type=int, default=3, help="sticky notes to add")
    ap.add_argument("--dag-id", default=None)
    ap.add_argument("--no-layout", action="store_true", help="leave all positions at 0/0")
    ap.add_argument("-o", "--output", type=Path, help="write here instead of stdout")
    args = ap.parse_args(argv)
    spec = large_dag_spec(
        args.tasks,
        group_size=args.group_size,
        seed=args.seed,
        layout=not args.no_layout,
        notes=args.notes,
        dag_id=args.dag_id,
    )
    text = json.dumps(spec, indent=1) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
        tasks = sum(1 for n in spec["nodes"] if n["kind"] not in ("task_group", "note"))
        print(
            f"{args.output}: {tasks} tasks, {len(spec['nodes'])} nodes, {len(spec['edges'])} edges",
            file=sys.stderr,
        )
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
