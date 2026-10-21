---
title: Policy
description: Organisation policy for DAGs built with AutoAir - the policy switches and pattern rules, secret detection, Python code restrictions, declarative rules, Open Policy Agent, and every point where policy is enforced.
---

Policy is a set of validation rules that every DAG must satisfy. Violations are reported as problems in the builder, and **errors block deploys**: a spec that fails validation is never written, pushed or stored as a proposal. Policy is configured by administrators only; users cannot relax it from the UI.

Policy complements [IAM](/docs/admin/iam): IAM decides *who* may do something, policy decides *what* a DAG may contain.

Three layers exist, from simple to fully programmable:

1. **Switches and pattern rules**: flat settings such as `AUTOAIR_REQUIRE_OWNER` or `AUTOAIR_FORBIDDEN_OPERATORS`, and the patterns of the `policy` section. Problems have the code `policy` (or `secret` for hard-coded secrets).
2. **Declarative rules** (`policy.rules`): data, never code. *Where* a rule applies and *what* must hold, with a severity. Problems have the code `policy_rule` and end with `[policy rule <id>]`.
3. **Open Policy Agent** (`policy.opa_url`): your own Rego policy decides. Problems have the code `policy_opa`.

## Where policy is enforced

The same policy object is built from the settings for every entry point, so all of them apply identical rules:

| Entry point | What happens on a violation |
|---|---|
| **Builder** (live validation, **Problems** panel, **Run → Validate in Airflow**) | Problems appear on the node or field; clicking one jumps to it. See [Validation](/docs/user-guide/validation) |
| **`POST /api/validate`** (optional `environment` query parameter) | The report lists the problems; `ok` is false when there is an error |
| **Deploy** (`PUT /api/specs/{dag_id}`) | `status: "invalid"`, "validation failed; nothing was written"; counted as `autoair_deploys_total{result="invalid"}` |
| **Deploy proposals** (`iam.require_approval`) | "validation failed; no proposal was stored". Deploying an approved proposal (`POST /api/specs/{dag_id}/proposals/{etag}/deploy`) validates the spec again with the current policy, so a rule added after the proposal was stored still blocks it |
| **`autoair validate spec.json [--env prod]`** | Prints each problem; exit code `1` when the spec fails |
| **MCP server tools** and **AI writers** | Generated or edited specs are validated with the same policy (`ai.guardrails.check_generated_code`) before they are presented as valid |
| **Operator palette** | `operator_allow` / `operator_deny` hide operators from the palette and catalog |

:::note Environment promotion is not a policy checkpoint
Promotion (`POST /api/vcs/{dag_id}/promote`, Git deploy targets with `storage.git.environments`) does **not** run validation or policy. It reads the spec stored on the source environment's branch, renders the DAG files again for the target environment (so that environment's overlay applies to the generated code) and opens a pull request to the target branch. Rules scoped to the target environment, for example `match: {environments: [prod]}`, are therefore not evaluated for the promoted DAG. To enforce them, validate before merging, for example by running `autoair validate <dag_id>.autoair.json --env prod` on the spec files of the promotion branch in the repository's CI, and require review of promotion pull requests. Promotions are still checked by IAM (the `promote` permission), rate-limited with deploys and recorded in the audit trail as `promote`.
:::

Some rules depend on the **environment** (such as `dev`, `staging` or `prod`). Validation knows it from the deploy request, the builder's environment selector (the live check follows it), `autoair validate --env`, the `environment` query parameter of `/api/validate`, or else `default_environment`. When the DAG defines an overlay for that environment, the overlay (including its connection and variable mapping) is applied before the rules run.

`GET /api/meta` publishes the effective policy (`policy`: `allow_python_code`, `allow_privileged_operators`, `require_owner`, `require_description`, `min_retries`, `required_tags`, `forbidden_operators`, `max_tasks`, `dag_id_prefix`, `python_ref_allow`, `dag_id_patterns`, `task_id_patterns`), so the UI can explain restrictions before validation runs.

Warnings (such as hard-coded secrets) are reported but do not block a deploy.

## Policy switches

| Variable | Default | Effect |
|---|---|---|
| `AUTOAIR_ALLOW_PYTHON_CODE` | `true` | `false`: Python code is refused (`@task` bodies, inline functions, embedded script files, Python references, loop `until` code). Also off when `features.python_code=false` |
| `AUTOAIR_ALLOW_PRIVILEGED_OPERATORS` | `true` | `false`: operators in `policy.privileged_operators` (BashOperator, BashSensor by default) are refused: "BashOperator is disabled by policy" |
| `AUTOAIR_FORBIDDEN_OPERATORS` | `[]` | fnmatch patterns of operator class paths DAGs may not use, for example `["*.SSHOperator", "airflow.providers.docker.*"]` |
| `AUTOAIR_REQUIRE_OWNER` | `false` | Every task needs an owner ("an owner is required (default_args.owner)") |
| `AUTOAIR_REQUIRE_DESCRIPTION` | `false` | The DAG needs a description or `doc_md` |
| `AUTOAIR_MIN_RETRIES` | unset | Minimum `retries` of every task |
| `AUTOAIR_REQUIRED_TAGS` | `[]` | Tags every DAG must carry |
| `AUTOAIR_MAX_TASKS` | unset | Most tasks a DAG may have |
| `AUTOAIR_DAG_ID_PREFIX` | unset | Prefix every `dag_id` must have |
| `AUTOAIR_TEAM_DAG_ID_PREFIX` | `false` | Multi-team: `dag_id` must start with `<team>_` |
| `AUTOAIR_PYTHON_REF_ALLOW` | `["*"]` | fnmatch patterns of Python objects DAGs may reference (needs `allow_python_code`), for example `["acme_airflow.*", "airflow.providers.*"]` |
| `AUTOAIR_OPERATOR_ALLOW` / `AUTOAIR_OPERATOR_DENY` | `["*"]` / `[]` | fnmatch patterns of operator class paths shown in / hidden from the palette |

`allow_python_code=false` also forbids functions written in place and embedded script files, because both end up as code in the DAG file.

## Pattern rules (`policy.*`)

The naming, privilege and secret-detection rules are settings of the [`policy` section](/docs/reference/configuration#policy). Each pattern can be **overridden** (the setting without `_EXTRA`; an empty value disables it) or **extended** (the matching `_EXTRA` setting):

| Variable | Default and meaning |
|---|---|
| `AUTOAIR_POLICY__PRIVILEGED_OPERATORS`, `AUTOAIR_POLICY__PRIVILEGED_OPERATORS_EXTRA` | fnmatch class paths that need `allow_privileged_operators` (BashOperator, BashSensor); extend with, for example, `["airflow.providers.ssh.operators.ssh.SSHOperator"]` |
| `AUTOAIR_POLICY__DAG_ID_PATTERN`, `AUTOAIR_POLICY__DAG_ID_PATTERNS_EXTRA` | Regexes every `dag_id` must fullmatch (none by default; Airflow's own rule `[A-Za-z0-9_.-]+`, at most 250 characters, always applies) |
| `AUTOAIR_POLICY__TASK_ID_PATTERN`, `AUTOAIR_POLICY__TASK_ID_PATTERNS_EXTRA` | The same for task ids |
| `AUTOAIR_POLICY__SECRET_PARAM_PATTERN`, `AUTOAIR_POLICY__SECRET_PARAM_PATTERNS_EXTRA` | Parameter names whose literal value is a hard-coded secret (`password`, `passwd`, `secret`, `token`, `api_key`, `access_key`, `private_key`, `credential`) |
| `AUTOAIR_POLICY__SECRET_PARAM_EXCLUDE_PATTERN`, `AUTOAIR_POLICY__SECRET_PARAM_EXCLUDE_PATTERNS_EXTRA` | Names that are never secrets (`*_conn_id`, `*_id`, `*_name`, `*_path`, `*_file`, `*_field`, `*_url`, `*_ref`, `token_url`) |
| `AUTOAIR_POLICY__SECRET_ASSIGNMENT_PATTERN`, `AUTOAIR_POLICY__SECRET_ASSIGNMENT_PATTERNS_EXTRA` | Secret-looking assignments in inline code and scripts (`password = "..."`, `export API_KEY='...'`) |
| `AUTOAIR_POLICY__JINJA_NAMES_EXTRA` | Template variables known besides Airflow's context (user-defined macros, plugin macros): no "unknown template variable" warning for them |

```yaml
# /etc/autoair/autoair.yaml
allow_privileged_operators: false
require_owner: true
min_retries: 1
required_tags: [team]
forbidden_operators: ["airflow.providers.docker.*"]
policy:
  dag_id_pattern: "[a-z][a-z0-9_]*"
  task_id_patterns_extra: ["[a-z][a-z0-9_]*"]
  privileged_operators_extra: ["airflow.providers.ssh.operators.ssh.SSHOperator"]
  jinja_names_extra: [company_calendar]
```

Regexes are compiled when the settings load; an invalid regex stops the server with an error naming the setting.

## Secret detection

Literal values of parameters whose name contains `password`, `passwd`, `secret`, `token`, `api_key`, `access_key`, `private_key` or `credential` (the default `policy.secret_param_pattern`), and secret-looking assignments in inline code produce a `secret` **warning** that tells the user to use `{{ conn.<id>.password }}`, `{{ var.value.<key> }}` or an Airflow connection instead. Tune the patterns with the `policy.secret_*` settings above. Secret findings never block a deploy; combine them with code review or the AI leak scan if they must.

## Operator templates

Operator templates (blueprints) can lock or require fields of the tasks created from them, for example a fixed `pool` or a required `owner`; validation reports violations as errors. This is per-template policy maintained by template authors; see [Operator templates](/docs/user-guide/operator-templates).

## Declarative rules (`policy.rules`)

A rule has a **match** that selects the DAGs (and optionally the tasks) it applies to, and a **check** that says what must hold. Each violation is a problem with code `policy_rule`.

```yaml
# /etc/autoair/autoair.yaml
policy:
  rules:
    - id: no-bash-in-prod
      message: BashOperator is not allowed in production
      match: {environments: [prod], operators: [BashOperator]}
      check: forbid

    - id: prod-retries
      match: {environments: [prod, staging]}
      check: {type: min_retries, value: 2}

    - id: owner-everywhere
      severity: warning
      check: require_owner

    - id: no-rm-rf
      match:
        params: [{name: bash_command, pattern: 'rm\s+-rf'}]
      check: forbid

    - id: sql-uses-warehouse
      match: {operators: ['*.SQLExecuteQueryOperator']}
      check: {type: param, condition: {name: conn_id, one_of: [warehouse, warehouse_ro]}}

    - id: hourly-no-catchup
      match: {schedule: ['@hourly', '*/15 * * * *']}
      check: {type: catchup, value: false}

    - id: team-tag
      match: {teams: ['*']}
      check: {type: tags_include, team: true}

    - id: bounded-runs
      match: {environments: [prod]}
      check: {type: dag_field, field: max_active_runs, present: true, max: 4}

    - id: execution-timeout
      match: {operators: ['*KubernetesPodOperator']}
      check: {type: task_field, field: execution_timeout_seconds, present: true, max: 7200}
```

The same as an environment variable (a JSON list):

```bash
AUTOAIR_POLICY__RULES='[{"id":"no-bash","match":{"operators":["BashOperator"]},"check":"forbid"}]'
```

Rules are checked when the configuration loads: an unknown check type, a field outside the lists below, an invalid regular expression or a duplicate id is a configuration error, and the server does not start.

### Rule fields

| Field | Description |
|---|---|
| `id` | Unique id: a letter or digit followed by letters, digits, `_`, `.`, `-` (at most 81 characters). Shown in the problem |
| `message` | Problem text (default: generated from the check) |
| `description` | Documentation only |
| `severity` | `error` (default; blocks deploys) or `warning` (reported only) |
| `enabled` | `false` switches the rule off |
| `match` | Where it applies (below); empty: every DAG |
| `check` (alias `assert`) | What must hold (below). A string is short for `{type: <string>}`: `check: forbid` |

### Match selectors

Every given selector must match; a list matches when any of its entries does.

| Selector | Matches |
|---|---|
| `environments` | fnmatch patterns of the environment. **A rule with `environments` does not apply when the environment is unknown** |
| `teams` | fnmatch patterns of the DAG's `team` |
| `dag_ids` | fnmatch patterns of `dag_id` |
| `tags` | The DAG carries at least one of these tags |
| `schedule` | A preset (`@daily` also matches `0 0 * * *` and vice versa), a cron expression, or a schedule type: `none`, `preset`, `cron`, `interval`, `multi_cron`, `events`, `assets`, `assets_or_time`, `assets_and_time`, `partitioned_cron`, `partitioned_assets` |
| `operators` | **Task selector.** fnmatch patterns of the operator class path; a pattern without a dot matches the class name (`BashOperator`). TaskFlow tasks are `@task` or `@task.<decorator>` (for example `@task.bash`); extension nodes are `custom:<handler>` |
| `params` | **Task selector.** Conditions on task parameters (operator `params`, TaskFlow `arguments` and `decorator_kwargs`, custom-node `config`): `{name, present, equals, one_of, pattern}`. With only `name`, the parameter has to be set; `pattern` is a regex searched in the value |

With a task selector, the rule applies to the matching (enabled) tasks only. Without one, task checks apply to every task of the DAG, and DAG checks to the DAG.

### Checks

| `type` | Holds when |
|---|---|
| `forbid` | Nothing matches (the matching tasks, or the DAG, are reported) |
| `require_owner` | Every selected task has an owner (its own or `default_args.owner`) |
| `min_retries` (`value`) | Every selected task retries at least `value` times (its own or `default_args`) |
| `catchup` (`value`, default `false`) | The DAG's `catchup` equals `value` |
| `tags_include` (`tags`, `team`) | The DAG has all `tags`, and its team name as a tag when `team: true` |
| `require_description` | The DAG has a description or `doc_md` |
| `max_tasks` (`value`) | At most `value` enabled tasks |
| `require_param` (`name`) | Every selected task sets parameter `name` |
| `param` (`condition`) | Every selected task's parameter satisfies the condition (`name` with `equals`, `one_of`, `pattern` or `present`) |
| `dag_field` (`field`, `present`, `equals`, `min`, `max`) | A DAG field is set, equals a value, or lies within bounds |
| `task_field` (`field`, `present`, `equals`, `min`, `max`) | The same for a field of every selected task |

`dag_field` fields: `description`, `doc_md`, `catchup`, `max_active_runs`, `max_active_tasks`, `dagrun_timeout_seconds`, `is_paused_upon_creation`, `fail_fast`, `max_consecutive_failed_dag_runs`, `render_template_as_native_obj`, `rerun_with_latest_version`, `timezone`, `start_date`, `end_date`, `team`, `default_args.owner`, `default_args.retries`, `default_args.retry_delay_seconds`, `default_args.execution_timeout_seconds`, `default_args.email`, `default_args.pool`, `default_args.queue`.

`task_field` fields: `owner`, `retries`, `retry_delay_seconds`, `execution_timeout_seconds`, `pool`, `pool_slots`, `queue`, `priority_weight`, `doc_md`, `executor`, `max_active_tis_per_dagrun`, `depends_on_past`, `trigger_rule`. `owner`, `retries`, `retry_delay_seconds`, `execution_timeout_seconds`, `pool`, `queue` and `depends_on_past` fall back to the DAG's `default_args`.

## Open Policy Agent

For policies that need full programmability, AutoAir can ask an [Open Policy Agent](https://www.openpolicyagent.org/) server during every validation.

```yaml
policy:
  opa_url: http://opa:8181/v1/data/autoair/violations
  opa_timeout: 2          # seconds
  opa_fail_mode: open     # open | closed
  opa_cache_seconds: 5    # reuse a decision for an identical input (0: always ask)
```

```bash
AUTOAIR_POLICY__OPA_TOKEN=...                                  # optional bearer token (secret)
AUTOAIR_POLICY__OPA_HEADERS='{"X-Tenant": "data-platform"}'    # optional extra headers
```

AutoAir POSTs the spec and context to the decision URL. TLS and proxy options for this call are the `opa` entry of `outbound.clients` (see [Configuration](/docs/admin/configuration#outgoing-connections-ca-bundle-client-certificates-proxies)).

```json
{"input": {
  "spec": {"...": "the DagSpec as JSON, after the environment overlay"},
  "context": {"environment": "prod", "team": "data", "user": "alice", "action": "deploy",
              "autoair_version": "0.1.0", "airflow_version": "3.3.2"}
}}
```

`context.user` and `context.action` are present for deploys (`deploy`), deploy previews (`deploy_preview`) and promotions between environments (`promote`, validated for the target environment). The decision (`result`) may be:

* `true` or `false`: allowed, or one error ("denied by the OPA policy");
* a list of messages, or of objects `{message, severity, node_id, field}` (one problem each; `error` unless the object says `warning`);
* an object with `deny` / `violations` (errors), `warn` / `warnings` (warnings) and/or `allow` (boolean);
* undefined (no `result`): nothing to report.

Example policy:

```rego
package autoair

import rego.v1

violations contains {"message": msg, "node_id": node.id} if {
  input.context.environment == "prod"
  some node in input.spec.nodes
  node.kind == "operator"
  endswith(node.operator, ".BashOperator")
  msg := sprintf("%s: BashOperator is not allowed in prod", [node.task_id])
}

violations contains {"message": "prod DAGs need a description", "severity": "warning"} if {
  input.context.environment == "prod"
  not input.spec.description
}
```

**When OPA cannot be asked** (timeout, connection error, HTTP 400 or above, invalid JSON), `opa_fail_mode` decides: `open` (default) reports one `policy_opa` **warning** and lets the DAG pass; `closed` reports an **error**, which blocks deploys until OPA answers again. Decisions for an identical input are cached for `opa_cache_seconds`, so live validation in the editor does not call OPA on every keystroke of an unchanged document. Each replica has its own cache.

| Setting | Default | Description |
|---|---|---|
| `policy.opa_url` | unset | Decision URL; unset: OPA is not asked |
| `policy.opa_timeout` | `2.0` | Seconds to wait for the decision |
| `policy.opa_fail_mode` | `open` | `open` or `closed` |
| `policy.opa_token` | unset | Bearer token (secret) |
| `policy.opa_headers` | `{}` | Extra HTTP headers (masked when the configuration is shown) |
| `policy.opa_cache_seconds` | `5.0` | Decision cache per identical input |

## Related pages

* [Validation](/docs/user-guide/validation): how users see problems
* [Configuration](/docs/admin/configuration): sources and file formats
* [Feature flags](/docs/admin/feature-flags): switching Python code off entirely
* [Configuration reference](/docs/reference/configuration#policy)
