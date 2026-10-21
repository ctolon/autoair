/**
 * Ready-to-paste snippets that trigger a deployed DAG through Airflow's own REST API
 * (`POST /api/v2/dags/{dag_id}/dagRuns`) or CLI — the "webhook" of an Airflow DAG.
 * The base URL is Airflow as API clients reach it (`/api/meta` `airflow_public_url`);
 * the token is left as the `AIRFLOW_TOKEN` environment variable.
 */

export type SnippetKind = "curl" | "python" | "javascript" | "cli";

export const SNIPPET_KINDS: readonly SnippetKind[] = ["curl", "python", "javascript", "cli"];

interface TriggerTarget {
  /** Airflow base URL (no trailing slash needed). */
  baseUrl: string;
  dagId: string;
  /** The run's conf (from the DAG params defaults); empty -> no conf. */
  conf: Record<string, unknown>;
}

export function dagRunsUrl(baseUrl: string, dagId: string): string {
  return `${baseUrl.replace(/\/+$/, "")}/api/v2/dags/${encodeURIComponent(dagId)}/dagRuns`;
}

/** The request body: `logical_date: null` is accepted by every Airflow 3.x. */
export function triggerBody(conf: Record<string, unknown>): Record<string, unknown> {
  return Object.keys(conf).length ? { logical_date: null, conf } : { logical_date: null };
}

/** POSIX shell single-quoting. */
export function shellQuote(text: string): string {
  return `'${text.replace(/'/g, `'\\''`)}'`;
}

/** A JSON value as a Python literal (True/False/None, repr-style strings). */
export function pythonLiteral(value: unknown, indent = 0): string {
  const pad = "    ".repeat(indent + 1);
  const end = "    ".repeat(indent);
  if (value === null || value === undefined) return "None";
  if (value === true) return "True";
  if (value === false) return "False";
  if (typeof value === "number") return Number.isFinite(value) ? String(value) : "None";
  if (typeof value === "string") return JSON.stringify(value);
  if (Array.isArray(value)) {
    if (!value.length) return "[]";
    return `[\n${value.map((v) => `${pad}${pythonLiteral(v, indent + 1)},`).join("\n")}\n${end}]`;
  }
  if (typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>);
    if (!entries.length) return "{}";
    return `{\n${entries
      .map(([k, v]) => `${pad}${JSON.stringify(k)}: ${pythonLiteral(v, indent + 1)},`)
      .join("\n")}\n${end}}`;
  }
  return "None";
}

export function buildSnippet(kind: SnippetKind, target: TriggerTarget): string {
  const url = dagRunsUrl(target.baseUrl, target.dagId);
  const body = triggerBody(target.conf);
  switch (kind) {
    case "curl":
      return [
        `curl -X POST ${shellQuote(url)} \\`,
        `  -H "Authorization: Bearer $AIRFLOW_TOKEN" \\`,
        `  -H 'Content-Type: application/json' \\`,
        `  -d ${shellQuote(JSON.stringify(body))}`,
      ].join("\n");
    case "python":
      return [
        "import os",
        "",
        "import requests",
        "",
        "response = requests.post(",
        `    ${JSON.stringify(url)},`,
        '    headers={"Authorization": f"Bearer {os.environ[\'AIRFLOW_TOKEN\']}"},',
        `    json=${pythonLiteral(body, 1)},`,
        "    timeout=30,",
        ")",
        "response.raise_for_status()",
        'print(response.json()["dag_run_id"])',
      ].join("\n");
    case "javascript":
      return [
        `const response = await fetch(${JSON.stringify(url)}, {`,
        '  method: "POST",',
        "  headers: {",
        "    Authorization: `Bearer ${process.env.AIRFLOW_TOKEN}`,",
        '    "Content-Type": "application/json",',
        "  },",
        `  body: JSON.stringify(${JSON.stringify(body, null, 2).replace(/\n/g, "\n  ")}),`,
        "});",
        "if (!response.ok) throw new Error(`Airflow: ${response.status} ${await response.text()}`);",
        "console.log((await response.json()).dag_run_id);",
      ].join("\n");
    case "cli": {
      const parts = ["airflow dags trigger", shellQuote(target.dagId)];
      if (Object.keys(target.conf).length) parts.push("--conf", shellQuote(JSON.stringify(target.conf)));
      return parts.join(" ");
    }
  }
}
