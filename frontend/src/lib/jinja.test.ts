import { describe, expect, it } from "vitest";

import { DAG_RUN_LOGICAL_DATE_ATTRS, LOGICAL_DATE_NAMES } from "./generated/jinjaContext.gen";
import {
  isTemplateString,
  parseExpression,
  renderPreview,
  sampleContext,
  splitTemplate,
  strftime,
  strptime,
  templateTimeVariables,
} from "./jinja";

const ctx = sampleContext({
  logicalDate: new Date(Date.UTC(2024, 0, 15, 6, 30, 0)),
  dagId: "etl",
  taskId: "load",
  params: { limit: 10, region: "eu", tables: ["a", "b"] },
  conf: { force: true },
  xcom: { extract: { return_value: { rows: 3 }, path: "/tmp/x.csv" } },
});
const render = (t: string) => renderPreview(t, ctx);

describe("splitTemplate", () => {
  it("splits text, expressions, statements and comments", () => {
    expect(splitTemplate("a {{ x }} b {% if y %}c{% endif %}{# note #}")).toEqual([
      { kind: "text", text: "a " },
      { kind: "expr", src: "x", raw: "{{ x }}" },
      { kind: "text", text: " b " },
      { kind: "stmt", src: "if y", raw: "{% if y %}" },
      { kind: "text", text: "c" },
      { kind: "stmt", src: "endif", raw: "{% endif %}" },
      { kind: "comment", raw: "{# note #}" },
    ]);
  });
  it("strips whitespace-control dashes", () => {
    expect(splitTemplate("{{- ds -}}")).toEqual([{ kind: "expr", src: "ds", raw: "{{- ds -}}" }]);
  });
});

describe("renderPreview: context variables", () => {
  it.each([
    ["{{ ds }}", "2024-01-15"],
    ["{{ ds_nodash }}", "20240115"],
    ["{{ ts }}", "2024-01-15T06:30:00+00:00"],
    ["{{ ts_nodash }}", "20240115T063000"],
    ["{{ ts_nodash_with_tz }}", "20240115T063000+0000"],
    ["{{ logical_date }}", "2024-01-15T06:30:00+00:00"],
    ["{{ data_interval_end }}", "2024-01-15T06:30:00+00:00"],
    ["{{ run_id }}", "scheduled__2024-01-15T06:30:00+00:00"],
    ["{{ dag.dag_id }}", "etl"],
    ["{{ dag_run.conf.force }}", "True"],
    ["{{ dag_run.conf['force'] }}", "True"],
    ["{{ ti.task_id }}", "load"],
    ["{{ task.task_id }}", "load"],
    ["{{ params.limit }}", "10"],
    ["{{ params['region'] }}", "eu"],
    ["{{ params.tables }}", "['a', 'b']"],
    ["{{ params.tables[-1] }}", "b"],
  ])("%s → %s", (tpl, out) => {
    expect(render(tpl).text).toBe(out);
  });
  it("keeps surrounding text", () => {
    expect(render("s3://bucket/{{ ds_nodash }}/data.csv").text).toBe("s3://bucket/20240115/data.csv");
  });
});

describe("renderPreview: methods, macros, filters, operators", () => {
  it.each([
    ["{{ logical_date.strftime('%Y/%m/%d %H:%M') }}", "2024/01/15 06:30"],
    ["{{ logical_date.year }}", "2024"],
    ["{{ logical_date.add(days=1) | ds }}", "2024-01-16"],
    ["{{ logical_date.subtract(months=1).strftime('%b') }}", "Dec"],
    ["{{ (logical_date - macros.timedelta(hours=7)) | ts_nodash }}", "20240114T233000"],
    ["{{ macros.ds_add(ds, 7) }}", "2024-01-22"],
    ["{{ macros.ds_add(ds, -15) }}", "2023-12-31"],
    ["{{ macros.ds_format(ds, '%Y-%m-%d', '%d.%m.%Y') }}", "15.01.2024"],
    ["{{ macros.datetime(2024, 2, 1) }}", "2024-02-01 00:00:00"],
    ["{{ params.region | upper }}", "EU"],
    ["{{ 'Hello World' | lower | replace('world', 'there') }}", "hello there"],
    ["{{ params.missing | default('x') }}", "⟨params.missing | default('x')⟩"],
    ["{{ none | default('fallback') }}", "fallback"],
    ["{{ params.tables | join(',') }}", "a,b"],
    ["{{ params.tables | length }}", "2"],
    ["{{ '  pad ' | trim }}", "pad"],
    ["{{ '42' | int + 1 }}", "43"],
    ["{{ params | tojson }}", '{"limit":10,"region":"eu","tables":["a","b"]}'],
    ["{{ 'abc' | title }}", "Abc"],
    ["{{ ds ~ '_' ~ params.limit }}", "2024-01-15_10"],
    ["{{ params.limit * 2 - 1 }}", "19"],
    ["{{ -params.limit }}", "-10"],
    ["{{ (1 + 2) * 3 }}", "9"],
    ["{{ 'a' if params.limit > 5 else 'b' }}", "a"],
    ["{{ params.region == 'eu' and not false }}", "True"],
    ["{{ 'a' in params.tables }}", "True"],
    ["{{ ds.replace('-', '') }}", "20240115"],
    ["{{ params.get('nope', 'dflt') }}", "dflt"],
    ["{{ [1, 2][0] }}", "1"],
    ["{{ {'k': 1}['k'] }}", "1"],
  ])("%s → %s", (tpl, out) => {
    const r = render(tpl);
    expect(r.errors).toEqual([]);
    expect(r.text).toBe(out);
  });
});

describe("renderPreview: XCom", () => {
  it("returns sample values when known", () => {
    expect(render("{{ ti.xcom_pull(task_ids='extract') }}").text).toBe("{'rows': 3}");
    expect(render("{{ ti.xcom_pull(task_ids='extract')['rows'] }}").text).toBe("3");
    expect(render("{{ ti.xcom_pull(task_ids='extract', key='path') }}").text).toBe("/tmp/x.csv");
    expect(render("{{ task_instance.xcom_pull('extract') }}").text).toBe("{'rows': 3}");
  });
  it("marks unknown outputs", () => {
    const r = render("x={{ ti.xcom_pull(task_ids='other') }}");
    expect(r.text).toBe("x=⟨ti.xcom_pull(task_ids='other')⟩");
    expect(r.segments[1]?.kind).toBe("unknown");
    expect(r.errors).toEqual([]);
  });
});

describe("renderPreview: unknowns and errors", () => {
  it("renders Variables / connections / unknown names as ⟨source⟩", () => {
    expect(render("{{ var.value.api_key }}").text).toBe("⟨var.value.api_key⟩");
    expect(render("{{ var.json.cfg.x | upper }}").text).toBe("⟨var.json.cfg.x | upper⟩");
    expect(render("{{ conn.pg.host }}:5432").text).toBe("⟨conn.pg.host⟩:5432");
    expect(render("{{ nope }}").segments[0]?.kind).toBe("unknown");
    expect(render("{{ params.limit + var.value.n }}").text).toBe("⟨params.limit + var.value.n⟩");
  });
  it("shows statements as placeholders and drops comments", () => {
    const r = render("{% for t in params.tables %}{{ t }}{% endfor %}{# c #}!");
    expect(r.segments.map((s) => s.kind)).toEqual(["statement", "unknown", "statement", "text"]);
  });
  it("reports syntax and evaluation errors without throwing", () => {
    const r = render("{{ ds | nosuchfilter }} {{ 1 + }} {{ 'x }}");
    expect(r.errors).toHaveLength(3);
    expect(r.segments.filter((s) => s.kind === "error")).toHaveLength(3);
  });
  it("never evaluates JavaScript", () => {
    expect(render("{{ constructor }}").segments[0]?.kind).toBe("unknown");
    expect(render("{{ params.constructor }}").segments[0]?.kind).toBe("unknown");
    expect(render("{{ params.__proto__ }}").segments[0]?.kind).toBe("unknown");
    expect(render("{{ ''.constructor }}").errors).toHaveLength(1);
  });
});

describe("helpers", () => {
  it("detects templates", () => {
    expect(isTemplateString("a {{ ds }}")).toBe(true);
    expect(isTemplateString("{% if x %}")).toBe(true);
    expect(isTemplateString("plain {text}")).toBe(false);
    expect(isTemplateString(null)).toBe(false);
  });
  it("finds logical-date dependent variables", () => {
    expect(
      templateTimeVariables("{{ ds }} {{ params.ds }} {{ dag_run.logical_date }} {{ macros.ds_add(ts, 1) }}").sort(),
    ).toEqual(["dag_run.logical_date", "ds", "ts"].sort());
    expect(templateTimeVariables("{{ params.x }} ds")).toEqual([]);
  });
  it("flags exactly the shared logical-date list", () => {
    for (const name of LOGICAL_DATE_NAMES) expect(templateTimeVariables(`{{ ${name} }}`)).toEqual([name]);
    for (const attr of DAG_RUN_LOGICAL_DATE_ATTRS)
      expect(templateTimeVariables(`{{ dag_run.${attr} }}`)).toEqual([`dag_run.${attr}`]);
    // Set for every Airflow 3 run, with or without a logical date.
    for (const name of ["task_instance_key_str", "prev_start_date_success", "prev_end_date_success", "run_id"])
      expect(templateTimeVariables(`{{ ${name} }}`)).toEqual([]);
  });
  it("strftime / strptime round trip", () => {
    const d = new Date(Date.UTC(2024, 6, 4, 13, 5, 9));
    expect(strftime(d, "%A %d %B %Y %I%p %j %%")).toBe("Thursday 04 July 2024 01PM 186 %");
    expect(strptime("04/07/2024", "%d/%m/%Y")?.toISOString()).toBe("2024-07-04T00:00:00.000Z");
    expect(strptime("bad", "%Y")).toBeNull();
  });
  it("parses keyword arguments", () => {
    expect(parseExpression("f(1, a=2)")).toMatchObject({
      k: "call",
      args: [{ k: "lit", v: 1 }],
      kwargs: [["a", { k: "lit", v: 2 }]],
    });
  });
});

describe("prototype safety", () => {
  it("does not resolve Object.prototype members", () => {
    expect(renderPreview("{{ ds | constructor }}", ctx).errors).toHaveLength(1);
    expect(renderPreview("{{ macros.timedelta(constructor=1) }}", ctx).errors).toHaveLength(1);
    expect(renderPreview("{{ ds.toString() }}", ctx).errors).toHaveLength(1);
  });
});

describe("date helpers", () => {
  it("start_of / end_of", () => {
    expect(renderPreview("{{ logical_date.start_of('month') }}", ctx).text).toBe("2024-01-01T00:00:00+00:00");
    expect(renderPreview("{{ logical_date.end_of('month') | ds }}", ctx).text).toBe("2024-01-31");
    expect(renderPreview("{{ logical_date.start_of('year') | ds }}", ctx).text).toBe("2024-01-01");
  });
});
