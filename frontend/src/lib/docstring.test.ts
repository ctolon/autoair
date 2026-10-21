import { describe, expect, it } from "vitest";

import { docstringToMarkdown, inlineRst, parseDocstring } from "./docstring";

const PYTHON_OPERATOR = `Base class for all Python operators.

    .. seealso::
        For more information on how to use this operator, take a look at the guide:
        :ref:\`howto/operator:PythonOperator\`

    When running your callable, Airflow will pass a set of keyword arguments that can be used in your
    function. For this to work, you need to define \`\`**kwargs\`\` in your function header.

    With explicit arguments:

    .. code-block:: python

       def my_python_callable(ti):
           pass

    :param python_callable: A reference to an object that is callable
    :param op_args: a list of positional arguments that will get unpacked when
        calling your callable
    :param templates_dict: a dictionary where the values are templates that
        will get templated by the Airflow engine. (templated)
    :param show_return_value_in_logs: a bool value whether to show return_value
        logs. Defaults to True.
    `;

describe("docstring → markdown", () => {
  it("renders the PythonOperator docstring like an IDE hover", () => {
    const doc = parseDocstring(PYTHON_OPERATOR);
    expect(doc.params.map((p) => p.name)).toEqual([
      "python_callable",
      "op_args",
      "templates_dict",
      "show_return_value_in_logs",
    ]);
    expect(doc.params[1]?.description).toBe(
      "a list of positional arguments that will get unpacked when calling your callable",
    );
    expect(doc.body).toContain("> **See also**");
    expect(doc.body).toContain("_PythonOperator_");
    expect(doc.body).toContain("`**kwargs`");
    expect(doc.body).toContain("```python\ndef my_python_callable(ti):\n    pass\n```");
    expect(doc.body).not.toContain(":param");
    const md = docstringToMarkdown(PYTHON_OPERATOR);
    expect(md).toContain("#### Parameters");
    expect(md).toContain("- **`python_callable`** — A reference to an object that is callable");
  });

  it("merges :type: and handles returns/raises", () => {
    const doc = parseDocstring(
      "Do it.\n\n:param int n: count\n:param s: name\n:type s: str\n:return: the result\n:rtype: dict\n:raises ValueError: when n < 0",
    );
    expect(doc.params).toEqual([
      { name: "n", type: "int", description: "count" },
      { name: "s", type: "str", description: "name" },
    ]);
    expect(doc.returns).toContain("the result");
    expect(doc.raises).toEqual([{ type: "ValueError", description: "when n < 0" }]);
  });

  it("parses Google style", () => {
    const doc = parseDocstring(
      "Summary.\n\nArgs:\n    path (str): Where to write.\n    retries (int): How often\n        to retry.\n\nReturns:\n    bool: True on success.\n\nRaises:\n    OSError: On I/O failure.",
    );
    expect(doc.params).toEqual([
      { name: "path", type: "str", description: "Where to write." },
      { name: "retries", type: "int", description: "How often to retry." },
    ]);
    expect(doc.returns).toBe("`bool` — True on success.");
    expect(doc.raises[0]?.type).toBe("OSError");
    expect(doc.body).toBe("Summary.");
  });

  it("parses NumPy style", () => {
    const doc = parseDocstring(
      "Summary.\n\nParameters\n----------\nx : int\n    The x.\ny, z : float\n    Coordinates.\n\nReturns\n-------\nfloat\n    The sum.",
    );
    expect(doc.params.map((p) => [p.name, p.type])).toEqual([
      ["x", "int"],
      ["y", "float"],
      ["z", "float"],
    ]);
    expect(doc.returns).toContain("The sum.");
  });

  it("parses Epytext", () => {
    const doc = parseDocstring("Summary.\n\n@param url: target URL\n@type url: str\n@return: the body");
    expect(doc.params).toEqual([{ name: "url", type: "str", description: "target URL" }]);
    expect(doc.returns).toBe("the body");
  });

  it("converts inline roles and links", () => {
    expect(inlineRst(":class:`~airflow.models.BaseOperator`")).toBe("`BaseOperator`");
    expect(inlineRst(":func:`airflow.sdk.task`")).toBe("`airflow.sdk.task`");
    expect(inlineRst("`Docs <https://airflow.apache.org>`_")).toBe("[Docs](https://airflow.apache.org)");
    expect(inlineRst(":ref:`the guide <howto/operator:Bash>`")).toBe("_the guide_");
  });

  it("turns :: paragraphs into code blocks", () => {
    const doc = parseDocstring("Example::\n\n    run(1)\n    run(2)\n\nDone.");
    expect(doc.body).toBe("Example:\n\n```python\nrun(1)\nrun(2)\n```\n\nDone.");
  });

  it("keeps the return type when :rtype: comes first", () => {
    expect(parseDocstring("Do.\n\n:rtype: int\n:return: the count").returns).toBe("the count (`int`)");
  });

  it("parses params whose description contains colons and roles", () => {
    const doc = parseDocstring(
      "Waits.\n\n:param bucket_key: The key(s) being waited on. Supports full s3:// style url or relative path.\n" +
        ":param kubernetes_conn_id: The :ref:`kubernetes connection id <howto/connection:kubernetes>`\n" +
        "    for the Kubernetes cluster.",
    );
    expect(doc.params.map((p) => p.name)).toEqual(["bucket_key", "kubernetes_conn_id"]);
    expect(doc.params[0]?.description).toContain("s3:// style url");
    expect(doc.params[1]?.description).toBe("The _kubernetes connection id_ for the Kubernetes cluster.");
  });
});
