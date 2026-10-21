import {
  type Completion,
  type CompletionContext,
  type CompletionResult,
  snippetCompletion,
} from "@codemirror/autocomplete";
import { type Diagnostic, linter } from "@codemirror/lint";
import { EditorState, type Extension } from "@codemirror/state";

import { shellLint } from "../../api/gen/sdk.gen";

/**
 * Shell "IntelliSense" without a language server: keywords, builtins, common CLI tools,
 * Airflow's task environment variables and the variables the script itself declares, plus
 * snippets for control flow. Diagnostics come from ShellCheck on the server when installed
 * (`autoair[shell]`); without it the editor shows none.
 */

const KEYWORDS = [
  "if",
  "then",
  "else",
  "elif",
  "fi",
  "for",
  "while",
  "until",
  "do",
  "done",
  "case",
  "esac",
  "in",
  "function",
  "select",
  "return",
  "time",
];

const BUILTINS: Record<string, string> = {
  echo: "Print arguments",
  printf: "Formatted output",
  read: "Read a line from stdin",
  cd: "Change directory",
  pwd: "Print working directory",
  export: "Export a variable to child processes",
  local: "Function-local variable",
  readonly: "Read-only variable",
  declare: "Declare variables/attributes",
  set: "Shell options (set -euo pipefail)",
  unset: "Remove a variable",
  source: "Run a file in this shell",
  test: "Evaluate a condition ([ … ])",
  exit: "Exit with a status (non-zero fails the task)",
  trap: "Run a command on signal/exit",
  shift: "Shift positional parameters",
  wait: "Wait for background jobs",
  eval: "Evaluate arguments as a command",
  exec: "Replace the shell with a command",
  true: "Exit status 0",
  false: "Exit status 1",
};

const COMMANDS: Record<string, string> = {
  ls: "List files",
  cp: "Copy files",
  mv: "Move/rename files",
  rm: "Remove files",
  mkdir: "Create directories (-p: parents)",
  cat: "Concatenate files",
  grep: "Search text",
  sed: "Stream editor",
  awk: "Pattern scanning",
  find: "Find files",
  xargs: "Build commands from stdin",
  sort: "Sort lines",
  uniq: "Drop duplicate lines",
  head: "First lines",
  tail: "Last lines",
  wc: "Count lines/words",
  cut: "Cut columns",
  tr: "Translate characters",
  tee: "Copy stdin to files",
  date: "Date/time (prefer {{ ds }} in Airflow)",
  sleep: "Wait",
  env: "Environment",
  basename: "File name of a path",
  dirname: "Directory of a path",
  chmod: "Change permissions",
  touch: "Create/refresh a file",
  ln: "Create links",
  tar: "Archives",
  gzip: "Compress",
  gunzip: "Decompress",
  zip: "Zip archive",
  unzip: "Extract zip",
  curl: "HTTP client (use --fail to fail the task on errors)",
  wget: "Download files",
  jq: "JSON processor",
  python: "Python interpreter",
  python3: "Python 3 interpreter",
  pip: "Python packages",
  aws: "AWS CLI",
  gsutil: "Google Cloud Storage CLI",
  gcloud: "Google Cloud CLI",
  az: "Azure CLI",
  kubectl: "Kubernetes CLI",
  docker: "Docker CLI",
  psql: "PostgreSQL client",
  mysql: "MySQL client",
  "spark-submit": "Submit a Spark application",
  dbt: "dbt CLI",
  airflow: "Airflow CLI",
  hdfs: "HDFS CLI",
  ssh: "Remote shell",
  scp: "Remote copy",
  rsync: "Sync files",
  git: "Git",
  make: "Build tool",
  timeout: "Run with a time limit",
};

/** Variables Airflow sets for every task process (plus common shell ones). */
const ENV_VARS: Record<string, string> = {
  AIRFLOW_CTX_DAG_ID: "DAG id of the running task",
  AIRFLOW_CTX_TASK_ID: "Task id",
  AIRFLOW_CTX_DAG_RUN_ID: "DAG run id",
  AIRFLOW_CTX_LOGICAL_DATE: "Logical date (ISO)",
  AIRFLOW_CTX_TRY_NUMBER: "Attempt number",
  AIRFLOW_CTX_DAG_OWNER: "DAG owner",
  AIRFLOW_CTX_DAG_EMAIL: "DAG e-mails",
  AIRFLOW_HOME: "Airflow home directory",
  HOME: "Home directory",
  PATH: "Executable search path",
  PWD: "Current directory",
  USER: "Current user",
  TMPDIR: "Temporary directory",
};

const SNIPPETS: Completion[] = [
  snippetCompletion("set -euo pipefail\n", {
    label: "strict",
    detail: "Fail on errors, unset vars, pipes",
    type: "keyword",
    boost: 2,
  }),
  snippetCompletion("if [[ ${condition} ]]; then\n\t${}\nfi", {
    label: "if",
    detail: "if … then … fi",
    type: "keyword",
  }),
  snippetCompletion("for ${item} in ${items}; do\n\t${}\ndone", { label: "for", detail: "for loop", type: "keyword" }),
  snippetCompletion("while ${condition}; do\n\t${}\ndone", { label: "while", detail: "while loop", type: "keyword" }),
  snippetCompletion('case "${value}" in\n\t${pattern})\n\t\t${}\n\t\t;;\n\t*)\n\t\t;;\nesac', {
    label: "case",
    detail: "case … esac",
    type: "keyword",
  }),
  snippetCompletion("${name}() {\n\t${}\n}", { label: "function", detail: "Function", type: "keyword" }),
  snippetCompletion("trap '${cleanup}' EXIT", { label: "trap", detail: "Cleanup on exit", type: "keyword" }),
  snippetCompletion("cat <<'EOF'\n${}\nEOF", { label: "heredoc", detail: "Here-document", type: "keyword" }),
];

/** Variables the script declares: `NAME=`, `export NAME=`, `local NAME`, `for NAME in`, `read NAME`. */
export function declaredVariables(text: string): string[] {
  const names = new Set<string>();
  const re =
    /(?:^|[\s;])(?:export\s+|local\s+|readonly\s+|declare\s+(?:-\w+\s+)?)?([A-Za-z_]\w*)=|\bfor\s+([A-Za-z_]\w*)\s+in\b|\bread\s+(?:-\w+\s+)*([A-Za-z_]\w*)/gm;
  for (const m of text.matchAll(re)) {
    const name = m[1] ?? m[2] ?? m[3];
    if (name) names.add(name);
  }
  return [...names];
}

function bashCompletions(ctx: CompletionContext): CompletionResult | null {
  // $VAR / ${VAR
  const variable = ctx.matchBefore(/\$\{?[A-Za-z_]*\w*$/);
  if (variable) {
    const braced = variable.text.startsWith("${");
    const from = variable.from + (braced ? 2 : 1);
    const own = declaredVariables(ctx.state.doc.toString());
    return {
      from,
      options: [
        ...own.map((v) => ({ label: v, type: "variable", detail: "script variable", boost: 1 })),
        ...Object.entries(ENV_VARS).map(([label, detail]) => ({ label, type: "constant", detail })),
      ],
      validFor: /^\w*$/,
    };
  }
  const word = ctx.matchBefore(/[\w-]+$/);
  if (!word && !ctx.explicit) return null;
  // Inside Jinja the template completion source answers instead.
  const before = ctx.state.sliceDoc(Math.max(0, ctx.pos - 200), ctx.pos);
  if (before.lastIndexOf("{{") > before.lastIndexOf("}}")) return null;
  return {
    from: word ? word.from : ctx.pos,
    options: [
      ...SNIPPETS,
      ...KEYWORDS.map((k) => ({ label: k, type: "keyword" })),
      ...Object.entries(BUILTINS).map(([label, detail]) => ({ label, type: "function", detail, boost: -1 })),
      ...Object.entries(COMMANDS).map(([label, detail]) => ({ label, type: "method", detail, boost: -2 })),
    ],
    validFor: /^[\w-]*$/,
  };
}

/** ShellCheck through the API (debounced by the lint extension); silent when unavailable. */
const shellcheck = linter(
  async (view) => {
    const code = view.state.doc.toString();
    if (!code.trim()) return [];
    try {
      const { data } = await shellLint({ body: { code, shell: "bash" }, throwOnError: true });
      if (!data.available) return [];
      const doc = view.state.doc;
      return (data.diagnostics ?? []).map((d): Diagnostic => {
        const line = doc.line(Math.min(Math.max(1, d.line), doc.lines));
        const from = Math.min(line.from + Math.max(0, (d.column ?? 1) - 1), line.to);
        const endLine = d.end_line ? doc.line(Math.min(Math.max(1, d.end_line), doc.lines)) : line;
        const to = Math.max(from, Math.min(endLine.from + Math.max(0, (d.end_column ?? 1) - 1), endLine.to));
        return { from, to, severity: d.severity, message: `${d.message} (${d.code})`, source: "ShellCheck" };
      });
    } catch {
      return [];
    }
  },
  { delay: 700 },
);

export function bashLanguageServices(): Extension[] {
  return [EditorState.languageData.of(() => [{ autocomplete: bashCompletions }]), shellcheck];
}
