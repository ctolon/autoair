"""Code mode: edited DAG code synced back into the spec (autoair.importer.code_sync)."""

from __future__ import annotations

from typing import Any

from autoair.importer.code_sync import (
    carry_canvas_state,
    error_line,
    short_error,
    sync_code,
    syntax_problem,
)
from autoair.importer.python_dag import ImportedDag, PythonImportResult
from autoair.spec import DagSpec


def spec(**over: Any) -> DagSpec:
    data: dict[str, Any] = {
        "dag_id": "d",
        "nodes": [
            {
                "id": "n1",
                "kind": "operator",
                "task_id": "extract",
                "operator": "airflow.providers.standard.operators.bash.BashOperator",
                "params": {"bash_command": "echo 1"},
                "position": {"x": 10, "y": 20},
                "label": "Extract!",
            },
            {
                "id": "n2",
                "kind": "operator",
                "task_id": "load",
                "operator": "airflow.providers.standard.operators.bash.BashOperator",
                "params": {"bash_command": "echo 2"},
                "position": {"x": 300, "y": 20},
            },
            {"id": "note1", "kind": "note", "text": "remember", "position": {"x": 0, "y": 200}},
        ],
        "edges": [{"source": "n1", "target": "n2"}],
        "team": "data",
    }
    data.update(over)
    return DagSpec.model_validate(data)


def imported() -> DagSpec:
    """What the importer returns for the same DAG: its own ids, no canvas state."""
    return DagSpec.model_validate(
        {
            "dag_id": "d",
            "nodes": [
                {
                    "id": "extract",
                    "kind": "operator",
                    "task_id": "extract",
                    "operator": "airflow.providers.standard.operators.bash.BashOperator",
                    "params": {"bash_command": "echo 1"},
                },
                {
                    "id": "load",
                    "kind": "operator",
                    "task_id": "load",
                    "operator": "airflow.providers.standard.operators.bash.BashOperator",
                    "params": {"bash_command": "echo changed"},
                },
                {
                    "id": "n1",  # collides with a kept id of another node
                    "kind": "operator",
                    "task_id": "new_task",
                    "operator": "airflow.providers.standard.operators.empty.EmptyOperator",
                },
            ],
            "edges": [
                {"source": "extract", "target": "load"},
                {"source": "load", "target": "n1"},
            ],
        }
    )


def test_syntax_errors_are_located_without_running_the_code():
    p = syntax_problem("x = 1\ndef f(:\n  pass\n")
    assert p is not None
    assert p.severity == "error"
    assert p.line == 2
    assert syntax_problem("x = 1\n") is None


def test_error_line_takes_the_users_file_from_a_traceback():
    tb = (
        'Traceback (most recent call last):\n  File "/usr/lib/x.py", line 5, in f\n'
        '  File "/tmp/autoair-import-1/d.py", line 12, in <module>\n'
        '  File "/tmp/autoair-import-1/d.py", line 14, in <module>\nNameError: boom'
    )
    assert error_line(tb, "d.py") == 14
    assert short_error(tb) == "NameError: boom"
    assert short_error("plain message") == "plain message"
    assert error_line("no trace", "d.py") is None


def test_canvas_state_ids_notes_and_autoair_fields_are_kept():
    out = carry_canvas_state(imported(), spec())
    by_task = {getattr(n, "task_id", None): n for n in out.nodes}
    extract, load, new = by_task["extract"], by_task["load"], by_task["new_task"]
    assert extract.id == "n1"
    assert extract.label == "Extract!"
    assert (extract.position.x, extract.position.y) == (10, 20)
    assert load.id == "n2"
    assert load.params["bash_command"] == "echo changed"
    assert new.id not in {"n1", "n2"}
    assert {(e.source, e.target) for e in out.edges} == {("n1", "n2"), ("n2", new.id)}
    assert any(n.kind == "note" for n in out.nodes)
    assert out.team == "data"


def fake_importer(result: PythonImportResult):
    def run(source: str, filename: str, timeout: float = 90) -> PythonImportResult:
        return result

    return run


def test_sync_reports_the_diff_against_the_current_document():
    result = PythonImportResult(
        dags=[ImportedDag(dag_id="d", spec=imported(), warnings=["opaque thing"])],
        import_errors=[],
    )
    out = sync_code("x = 1\n", current=spec(), dag_id="d", importer=fake_importer(result))
    assert out.ok
    assert out.spec is not None
    assert out.diff is not None
    assert [n["task_id"] for n in out.diff.added_nodes] == ["new_task"]
    assert [p.severity for p in out.problems] == ["warning"]


def test_sync_errors_keep_no_spec():
    nothing = PythonImportResult(dags=[], import_errors=[])
    syntax = sync_code("def (", importer=fake_importer(nothing))
    assert not syntax.ok
    assert syntax.problems[0].line == 1
    empty = sync_code("x = 1\n", importer=fake_importer(nothing))
    assert not empty.ok
    assert "no DAG" in empty.problems[0].message
    broken = PythonImportResult(
        dags=[], import_errors=['File "/tmp/a/dag.py", line 3, in <module>\nNameError: x']
    )
    failed = sync_code("x = 1\n", importer=fake_importer(broken))
    assert not failed.ok
    assert failed.problems[0].line == 3


def test_sync_picks_the_requested_dag_and_warns_about_others():
    other = DagSpec.model_validate({"dag_id": "other"})
    result = PythonImportResult(
        dags=[
            ImportedDag(dag_id="other", spec=other, warnings=[]),
            ImportedDag(dag_id="d", spec=imported(), warnings=[]),
        ],
        import_errors=[],
    )
    out = sync_code("x = 1\n", dag_id="d", importer=fake_importer(result))
    assert out.spec is not None
    assert out.spec.dag_id == "d"
    assert out.dag_ids == ["other", "d"]
    assert "ignored" in out.problems[0].message


def test_notes_included_in_dag_docs_are_not_read_back_into_doc_md():
    current = spec(
        doc_md="Intro",
        nodes=[
            *spec().model_dump(mode="json", by_alias=True)["nodes"][:2],
            {"id": "note1", "kind": "note", "text": "First\n\nnote", "include_in_docs": True},
            {"id": "note2", "kind": "note", "text": "Second", "include_in_docs": True},
            {"id": "note3", "kind": "note", "text": "Canvas only"},
        ],
    )
    code_doc = "Intro\n\nFirst\n\nnote\n\nSecond"  # what codegen writes into doc_md
    merged = carry_canvas_state(imported().model_copy(update={"doc_md": code_doc}), current)
    assert merged.doc_md == "Intro"
    only_notes = imported().model_copy(update={"doc_md": "First\n\nnote\n\nSecond"})
    assert carry_canvas_state(only_notes, current).doc_md is None


def test_strip_note_docs_handles_text_edited_around_the_notes():
    from autoair.importer.code_sync import strip_note_docs

    assert strip_note_docs("A\n\nN1\n\nB\n\nN2", ["N1", "N2"]) == "A\n\nB"
    assert strip_note_docs("A", ["N1"]) == "A"
    assert strip_note_docs(None, ["N1"]) is None
    assert strip_note_docs("A\n\nN1", []) == "A\n\nN1"
