import { describe, expect, it } from "vitest";

import { buildSnippet, dagRunsUrl, pythonLiteral, shellQuote, triggerBody } from "./triggerSnippets";

const target = { baseUrl: "https://airflow.example.com/", dagId: "etl.daily", conf: { day: "it's", n: 2, on: true } };

describe("trigger snippets", () => {
  it("targets Airflow's dagRuns endpoint", () => {
    expect(dagRunsUrl("https://a/", "x/y")).toBe("https://a/api/v2/dags/x%2Fy/dagRuns");
    expect(triggerBody({})).toEqual({ logical_date: null });
    expect(triggerBody({ a: 1 })).toEqual({ logical_date: null, conf: { a: 1 } });
  });

  it("quotes for the shell", () => {
    expect(shellQuote("it's")).toBe("'it'\\''s'");
  });

  it("builds curl with the conf as JSON", () => {
    const s = buildSnippet("curl", target);
    expect(s).toContain("curl -X POST 'https://airflow.example.com/api/v2/dags/etl.daily/dagRuns'");
    expect(s).toContain("Bearer $AIRFLOW_TOKEN");
    expect(s).toContain(`-d '{"logical_date":null,"conf":{"day":"it'\\''s","n":2,"on":true}}'`);
  });

  it("builds Python requests with Python literals", () => {
    const s = buildSnippet("python", target);
    expect(s).toContain("import requests");
    expect(s).toContain('"logical_date": None,');
    expect(s).toContain('"on": True,');
    expect(pythonLiteral({ a: [1, null, false], b: {} })).toBe(
      '{\n    "a": [\n        1,\n        None,\n        False,\n    ],\n    "b": {},\n}',
    );
  });

  it("builds JS fetch and the Airflow CLI", () => {
    const js = buildSnippet("javascript", target);
    expect(js).toContain('method: "POST"');
    expect(js).toContain('"logical_date": null');
    expect(buildSnippet("cli", target)).toBe(
      `airflow dags trigger 'etl.daily' --conf '{"day":"it'\\''s","n":2,"on":true}'`,
    );
    expect(buildSnippet("cli", { ...target, conf: {} })).toBe("airflow dags trigger 'etl.daily'");
  });
});
