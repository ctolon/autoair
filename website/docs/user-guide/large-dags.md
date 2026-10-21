---
title: Large DAGs
description: How the editor handles DAGs with hundreds or thousands of tasks - viewport rendering, the low-detail overview, slower live checks, measured limits, and the settings to tune.
sidebar_label: Large DAGs
---

The editor is tested with DAGs of 500 tasks (about 700 dependencies) and 1,000 tasks (about 1,400 dependencies). Both stay usable: panning, zooming and dragging run at 60 frames per second on a current laptop, and the live checks answer in well under a second. This page describes what changes when a DAG gets large, the measured numbers, and what to tune when your DAGs are bigger still.

## What changes on a large DAG

All of these switch on by themselves; there is nothing to enable.

| Behaviour | Applies above | Effect |
|---|---|---|
| Viewport rendering | 150 nodes | Only nodes and edges inside the visible area are rendered. Panning mounts the ones that come into view. |
| Zoom out to an overview | 150 nodes | The canvas can zoom out to 5 % (instead of 50 %), so **Fit view** (**Shift+F**, the zoom controls or the context menu) shows the whole graph. |
| Low-detail cards | zoom below 40 % | Task cards turn into plain boxes with the task name, tinted with the colour of their kind. Errors, warnings, run states, the version diff and the selection stay visible as border and fill colours. Dependencies are drawn without their label and **+** button. Zooming back in restores the full cards. |
| Readable opening | 150 nodes | Opening a DAG, auto layout and resizing panels fit the view no further out than 50 %, centred on the graph, as before. Only **Fit view** zooms out to the overview. |
| Slower live checks | 200 nodes | The [live checks](/docs/user-guide/validation#live-checks) wait 600 ms plus 1 ms per node above 200 after your last change (at most 2 s): 900 ms at 500 nodes, 1.4 s at 1,000. Typing in the inspector then sends one check per pause, not one per keystroke. |

Undo and redo, copy and paste, collaboration, run-state overlays and the version diff overlay work the same on large DAGs. A drag is still one undo step. Collapsing task groups (the chevron in the group header) hides their members and is a quick way to work on one part of a large graph.

## Measured limits

Numbers from the benchmark below on one developer machine (production build, headless Chrome, 1600×1000 window). Your numbers will differ; the ratios between rows are the useful part.

| Step | 500 tasks | 1,000 tasks |
|---|---:|---:|
| Open the DAG until the first card shows | 0.7 s | 0.9 s |
| Open until the first live check is back | 1.5 s | 2.2 s |
| Server: validation, first time / again | 145 ms / 46 ms | 278 ms / 101 ms |
| Server: code generation | 32 ms | 65 ms |
| Auto layout (ELK, in a Web Worker) | 0.4–0.9 s | 0.9 s |
| Open a DAG without positions (laid out on open) | 1.5 s | 2.2 s |
| Pan and drag at a readable zoom | 60 fps | 60 fps |
| Zoom out to the overview (mounts every card) | short pauses, 0.2 s in total | short pauses, 0.4 s in total |
| Browser memory (JS heap) after opening | 27 MB | 30 MB |

"Again" is a later check of the same DAG: unchanged Python task bodies and shell scripts are not linted a second time (see below), so live checks while you edit cost the second number.

Beyond 1,000 tasks the editor keeps working, with costs that grow linearly: the server validates 2,000 tasks in about half a second the first time. The overview is the slowest view because it mounts every card; on very large graphs stay zoomed in or collapse task groups. The Airflow side has its own limits: a DAG file with thousands of tasks takes longer to parse in the DAG processor and to render in the Airflow UI.

## How validation scales

Validation lints the body of every Python task with ruff and every Bash command or shell script with ShellCheck. On a large DAG they run in batches over all bodies that changed since the last check (ruff: one process; ShellCheck: one process per 50 scripts), and the results of unchanged bodies are remembered by the server process. A run that fails or times out reports no findings for that validation and is retried on the next one. Before 0.2.0 each task started its own process, which took seconds on DAGs with hundreds of tasks.

## Tuning

| Setting | Default | When to change it |
|---|---|---|
| `AUTOAIR_EDITOR__PYTHON_LINT_TIMEOUT` | `15` | Seconds one ruff run may take. A first validation of thousands of Python tasks on a slow machine may need more; when it runs out, the lint findings are missing from that validation (nothing fails). |
| `AUTOAIR_EDITOR__SHELLCHECK_TIMEOUT` | `5` | The same for one ShellCheck run (up to 50 scripts), which is slower than ruff per script. |
| `AUTOAIR_EDITOR__SHELLCHECK_IN_VALIDATION` | `true` | `false` skips ShellCheck in validation entirely (the code editor still lints the script you are editing). |
| `AUTOAIR_SERVER__MAX_BODY_BYTES` | `16777216` | Largest request body. A 1,000-task spec is about 0.4 MB, so only very large or code-heavy DAGs get near it. |
| `AUTOAIR_IMPORT_CHECK_TIMEOUT` | `60` | Seconds of the Airflow import check (**Validate in Airflow**), which parses the generated file with Airflow. |
| `AUTOAIR_MAX_TASKS` | none | Organisation policy: refuse DAGs above a task count. |

The full list is in the [configuration reference](/docs/reference/configuration).

## Benchmarks

Two scripts measure large DAGs on your own machine. Both use the same synthetic DAGs: stages of twelve tasks in task groups, a mix of Bash, Python, empty, sensor and trigger tasks, mapped tasks over a literal list and over an upstream result, setup and teardown pairs, labelled dependencies and sticky notes.

```bash
# A spec to import or deploy (deterministic for a seed)
uv run python scripts/gen_large_dag.py --tasks 500 -o large.autoair.json

# Server: parse, validate, code generation, YAML and dag-factory export
uv run python scripts/bench_large_dag.py --tasks 500 --tasks 1000
uv run python scripts/bench_large_dag.py --catalog --import-check   # as the API, plus Airflow

# Browser: load, layout, frame rate while panning, zooming and dragging, memory
eval "$(.claude/skills/run-e2e/scripts/private-stack.sh start)"
cd frontend && PERF_SIZES=500,1000 npx playwright test e2e/large-graph.spec.ts --reporter=list
```

The browser benchmark writes its numbers to `.artifacts/perf/large-graph-<label>.json` (`PERF_LABEL`, default `run`) and prints a table; `PERF_PROFILE=1` also saves a CPU profile of the node drag that Chrome DevTools can open.

## Related pages

- [Canvas and workbench](/docs/user-guide/canvas)
- [Validation](/docs/user-guide/validation)
- [Scaling and replica safety](/docs/admin/scaling)
