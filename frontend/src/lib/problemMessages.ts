import type { Problem } from "../api/gen/types.gen";
import { i18n } from "../i18n";

type Rule = {
  /** Matched against the server's (English) message. */
  re: RegExp;
  /** Key under `lifecycle:problemText`. */
  key: string | ((p: Pick<Problem, "field" | "code">, m: RegExpExecArray) => string);
  /** Interpolation values from the match groups. */
  vars?: (m: RegExpExecArray) => Record<string, string>;
};

/** Which friendly explanation a schema "pattern" error on this field gets. */
function patternKey(p: Pick<Problem, "field">): string {
  const field = p.field ?? "";
  if (/(^|\.)(expression|cron|crons(\.\d+)?)$/.test(field) || field.startsWith("schedule")) return "patternCron";
  if (/(^|\.)(dag_id|task_id|group_id|function_name)$/.test(field)) return "patternId";
  if (/(exception|path|operator|notifier|callable)$/.test(field)) return "patternDotted";
  return "pattern";
}

const RULES: Rule[] = [
  { re: /^required parameter '([^']+)' is missing/, key: "requiredMissing", vars: (m) => ({ name: m[1] ?? "" }) },
  { re: /^required parameter '([^']+)' is empty/, key: "requiredEmpty", vars: (m) => ({ name: m[1] ?? "" }) },
  {
    re: /^unknown parameter '([^']+)' for (\S+)/,
    key: "unknownParam",
    vars: (m) => ({ name: m[1] ?? "", operator: m[2] ?? "" }),
  },
  { re: /^duplicate node id '([^']+)'/, key: "duplicateNode", vars: (m) => ({ id: m[1] ?? "" }) },
  {
    re: /^(\S+) is locked by operator template (.+?) \(expected (.*)\)$/,
    key: "templateLocked",
    vars: (m) => ({ field: m[1] ?? "", template: m[2] ?? "", expected: m[3] ?? "" }),
  },
  {
    re: /^(\S+) is required by operator template (.+)$/,
    key: "templateRequired",
    vars: (m) => ({ field: m[1] ?? "", template: m[2] ?? "" }),
  },
  {
    re: /^the task type is locked by operator template (.+)$/,
    key: "templateType",
    vars: (m) => ({ template: m[1] ?? "" }),
  },
  { re: /^operator template '([^']+)' no longer exists/, key: "templateMissing", vars: (m) => ({ id: m[1] ?? "" }) },
  {
    re: /^operator template (.+) was updated since this task was inserted \(v(.+)\)$/,
    key: "templateUpdated",
    vars: (m) => ({ template: m[1] ?? "", version: m[2] ?? "" }),
  },
  { re: /^dependency cycle: (.*)$/, key: "cycle", vars: (m) => ({ path: m[1] ?? "" }) },
  {
    re: /^task groups depend on each other in a cycle: (.*?)\. A dependency into or out of/,
    key: "taskGroupCycle",
    vars: (m) => ({ members: m[1] ?? "" }),
  },
  { re: /^end_date must be after start_date/, key: "dates" },
  { re: /^a node cannot depend on itself/, key: "selfLoop" },
  { re: /^notes cannot be connected/, key: "noteEdge" },
  { re: /^a task cannot consume its own output/, key: "xcomSelf" },
  { re: /^XCom reference to unknown node '([^']+)'/, key: "xcomUnknown", vars: (m) => ({ id: m[1] ?? "" }) },
  { re: /^a branch task needs at least one downstream task/, key: "branch" },
  { re: /^a loop group needs at least one task/, key: "loopEmpty" },
  // Astronomer Blueprint steps (autoair.validation.blueprints)
  {
    re: /^blueprint '([^']+)' v(\d+) is not in the blueprint catalog/,
    key: "blueprintUnknown",
    vars: (m) => ({ name: m[1] ?? "", version: m[2] ?? "" }),
  },
  {
    re: /^the Airflow environment needs airflow-blueprint and the module '([^']+)'/,
    key: "blueprintRuntime",
    vars: (m) => ({ module: m[1] ?? "" }),
  },
  { re: /^blueprint steps cannot be expressed in YAML Dags/, key: "blueprintOutputMode" },
  { re: /^blueprint steps cannot be placed in a loop group/, key: "blueprintLoop" },
  { re: /^blueprint steps are switched off/, key: "blueprintDisabled" },
  { re: /^blueprint config values must be plain JSON/, key: "blueprintPlainJson" },
  { re: /^a newer version of '([^']+)' is available/, key: "blueprintOutdated", vars: (m) => ({ name: m[1] ?? "" }) },
  { re: /^XCom must reference a task, not a blueprint step/, key: "xcomBlueprint" },
  // Raw code DAGs (autoair.validation.raw)
  { re: /^raw code DAGs are Python and need allow_python_code/, key: "rawPythonDisabled" },
  { re: /^the DAG file is empty/, key: "rawEmpty" },
  { re: /^this line assigns what looks like a hard-coded secret/, key: "rawSecret" },
  {
    re: /^the code does not define a DAG with dag_id '([^']+)' \(found: ([^)]*)\)/,
    key: "rawDagId",
    vars: (m) => ({ id: m[1] ?? "", found: m[2] ?? "" }),
  },
  {
    re: /^(\S+) runs arbitrary commands and is disabled by policy/,
    key: "rawPrivileged",
    vars: (m) => ({ name: m[1] ?? "" }),
  },
  { re: /^(\S+) is forbidden by policy$/, key: "rawForbidden", vars: (m) => ({ name: m[1] ?? "" }) },
  // The validator service (autoair.validation.dagbag_check.ImportCheckUnavailable)
  {
    re: /^the import check service is unavailable \((.*)\); deploys are blocked/,
    key: "importServiceClosed",
    vars: (m) => ({ reason: m[1] ?? "" }),
  },
  {
    re: /^the import check service is unavailable \((.*)\); the DAG was not imported/,
    key: "importServiceOpen",
    vars: (m) => ({ reason: m[1] ?? "" }),
  },
  {
    re: /^the file did not define DAG '([^']+)' \(found: ([^)]*)\)/,
    key: "rawImportMissing",
    vars: (m) => ({ id: m[1] ?? "", found: m[2] ?? "" }),
  },
  {
    re: /^dag_id must start with the team prefix '([^']+)'/,
    key: "dagIdTeamPrefix",
    vars: (m) => ({ prefix: m[1] ?? "" }),
  },
  { re: /^dag_id must start with '([^']+)'/, key: "dagIdPrefix", vars: (m) => ({ prefix: m[1] ?? "" }) },
  { re: /^dag_id must match '(.*)'$/, key: "dagIdPattern", vars: (m) => ({ pattern: m[1] ?? "" }) },
  { re: /^Python code tasks are disabled by policy/, key: "pythonDisabled" },
  { re: /needs allow_python_code/, key: "pythonDisabled" },
  { re: /^an owner is required/, key: "ownerRequired" },
  { re: /^a description is required/, key: "descriptionRequired" },
  { re: /^retries must be ≥ (\d+)/, key: "minRetries", vars: (m) => ({ n: m[1] ?? "" }) },
  { re: /^required tags missing: (.*)$/, key: "requiredTags", vars: (m) => ({ tags: m[1] ?? "" }) },
  { re: /^at most (\d+) tasks are allowed/, key: "maxTasks", vars: (m) => ({ n: m[1] ?? "" }) },
  { re: /^'([^']+)' looks like a hard-coded secret/, key: "secret", vars: (m) => ({ name: m[1] ?? "" }) },
  // Pydantic (422 schema errors)
  { re: /^String should match pattern '(.*)'$/, key: (p) => patternKey(p), vars: (m) => ({ pattern: m[1] ?? "" }) },
  { re: /^Field required$/, key: "fieldRequired" },
  { re: /^Input should be a valid (integer|number)/, key: "notNumber" },
  { re: /^Input should be a valid boolean/, key: "notBoolean" },
  { re: /^Input should be a valid string/, key: "notString" },
  {
    re: /^String should have at most (\d+) characters?/,
    key: "tooLong",
    vars: (m) => ({ n: m[1] ?? "" }),
  },
  {
    re: /^Input should be greater than or equal to (\S+)/,
    key: "tooSmall",
    vars: (m) => ({ n: m[1] ?? "" }),
  },
  { re: /^Extra inputs are not permitted/, key: "extraField" },
];

/**
 * A problem's message in the UI language, with friendly wording for raw schema errors
 * ("String should match pattern '^\S+( \S+){4,5}$'" → "A cron expression has 5 fields…").
 * Unknown messages are shown as the server sent them.
 */
/** Problems whose text the organisation wrote (policy.rules messages, OPA decisions). */
const VERBATIM = new Set(["policy_rule", "policy_opa"]);

export function problemText(p: Pick<Problem, "message" | "field" | "code">): string {
  if (VERBATIM.has(p.code)) return p.message;
  for (const rule of RULES) {
    const m = rule.re.exec(p.message);
    if (!m) continue;
    const key = typeof rule.key === "function" ? rule.key(p, m) : rule.key;
    return i18n.t(`problemText.${key}`, {
      ns: "lifecycle",
      ...(rule.vars?.(m) ?? {}),
      defaultValue: p.message,
    });
  }
  return p.message;
}
