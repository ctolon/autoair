from __future__ import annotations

import pytest

from autoair.lsp import PythonContext, complete, hover, signatures

pytestmark = [
    pytest.mark.airflow,
    # parso (jedi) grammar-cache races in worker threads surface as unraisable KeyErrors.
    pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning"),
]


def ctx(code: str, line: int, column: int, **kw: object) -> PythonContext:
    return PythonContext(code=code, line=line, column=column, **kw)  # type: ignore[arg-type]


def test_completes_airflow_imports():
    code = "from airflow.providers.standard.operators.bash import Bash"
    labels = [i.label for i in complete(ctx(code, 1, len(code))).items]
    assert "BashOperator" in labels


def test_completes_stdlib_with_prefix():
    code = "import json\nrows = json.lo"
    result = complete(ctx(code, 2, len("rows = json.lo")))
    assert {"load", "loads"} <= {i.label for i in result.items}
    assert result.prefix_length == 2


def test_arguments_and_loop_context_are_known():
    labels = [i.label for i in complete(ctx("return lim", 1, 10, arguments=["limit"])).items]
    assert "limit" in labels
    labels = [i.label for i in complete(ctx("i = lo", 1, 6, uses_loop_context=True)).items]
    assert "loop" in labels


def test_hover_and_signatures_use_airflow_docs():
    code = "from airflow.sdk import Variable\nVariable.get("
    info = hover(ctx(code, 1, 26))
    assert info.markdown is not None
    assert "Variable" in info.markdown
    sigs = signatures(ctx(code, 2, len("Variable.get("))).signatures
    assert sigs
    assert sigs[0].label.startswith("get(key")


def test_module_mode_for_inline_functions_and_scripts():
    code = "import os\n\ndef build(**context):\n    return os.envi"
    result = complete(ctx(code, 4, len("    return os.envi"), mode="module"))
    assert "environ" in {i.label for i in result.items}
    # The unfinished attribute access of the user's report still answers (no exception).
    code = "import os\nos.en"
    assert "environ" in {i.label for i in complete(ctx(code, 2, 5, mode="module")).items}


def test_shellcheck_lint_masks_jinja():
    from autoair.lsp.shell import ShellLintRequest, lint_shell, mask_jinja, shellcheck_path

    assert mask_jinja("echo {{ ds }}\n{% if x %}y{% endif %}") == (
        "echo xxxxxxxx\n          y           "
    )
    if shellcheck_path() is None:
        pytest.skip("shellcheck not installed (pip install autoair[shell])")
    result = lint_shell(
        ShellLintRequest(code="rm -rf $DIR/{{ ds }}\nif [ $x == 1 ]; then echo hi; fi")
    )
    assert result.available
    codes = {d.code for d in result.diagnostics}
    assert "SC2086" in codes  # unquoted $DIR
    first = next(d for d in result.diagnostics if d.code == "SC2086")
    assert first.line == 1  # the injected "# shellcheck shell=" line is accounted for
    assert lint_shell(ShellLintRequest(code="echo ok")).diagnostics == []
