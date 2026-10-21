# Policy rules and OPA

Reference for contributors and operators. Task-oriented guide on the documentation site: [Policy](https://ctolon.github.io/autoair/docs/admin/policy).

AutoAir validates every DAG against the organisation policy: in the editor (the **Problems**
panel, live), on `autoair validate`, through the API (`POST /api/validate`) and before every
deploy or deploy proposal. Three layers exist, from simple to fully programmable:

1. **Flat switches** (`AUTOAIR_REQUIRE_OWNER`, `AUTOAIR_REQUIRE_DESCRIPTION`,
   `AUTOAIR_MIN_RETRIES`, `AUTOAIR_REQUIRED_TAGS`, `AUTOAIR_FORBIDDEN_OPERATORS`,
   `AUTOAIR_MAX_TASKS`, `AUTOAIR_DAG_ID_PREFIX`) and the pattern settings of the `policy`
   section — see
   the [`policy` reference](configuration-reference.md#policy).
2. **Declarative rules** (`policy.rules`) — data, never code: *where* a rule applies and *what*
   must hold, with a severity. Described below.
3. **Open Policy Agent** (`policy.opa_url`) — your own Rego policy decides.

A problem with severity `error` blocks deploys; `warning` is shown only. Problems of
declarative rules have the code `policy_rule` and end with `[policy rule <id>]`; OPA's
have the code `policy_opa`. Clicking one in the Problems panel jumps to the task / field.

## The environment

Rules can target environments (such as `dev`, `staging` or `prod`). Validation knows the environment
from the deploy request, the builder's environment selector (the live check follows it), the
`--env` option of `autoair validate`, the `environment` query parameter of `/api/validate`, or
else `default_environment`. When the DAG defines an overlay for that environment
(`environments.<env>`), the overlay — including its connection/variable mapping — is applied
before the rules run. **A rule with `match.environments` does not apply when the environment
is unknown.**

## Declarative rules

```yaml
# autoair.yaml
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

The same as an environment variable: `AUTOAIR_POLICY__RULES='[{"id":"no-bash","match":{"operators":["BashOperator"]},"check":"forbid"}]'`.

### Rule fields

| Field | Meaning |
|---|---|
| `id` | unique id (letters, digits, `_ . -`), shown in the problem |
| `message` | problem text (default: generated from the check) |
| `description` | documentation only |
| `severity` | `error` (default, blocks deploys) or `warning` |
| `enabled` | `false` switches the rule off |
| `match` | where it applies (below); empty: every DAG |
| `check` (alias `assert`) | what must hold (below); a string is short for `{type: <string>}` |

### `match`

Every given selector must match; a list matches when any of its entries does.

| Selector | Matches |
|---|---|
| `environments` | fnmatch patterns of the environment (unknown environment: no match) |
| `teams` | fnmatch patterns of the DAG's `team` |
| `dag_ids` | fnmatch patterns of `dag_id` |
| `tags` | the DAG carries at least one of these tags |
| `schedule` | a preset (`@daily` also matches `0 0 * * *` and vice versa), a cron expression, or a schedule type (`none`, `preset`, `cron`, `interval`, `multi_cron`, `events`, `assets`, `assets_or_time`, `assets_and_time`, `partitioned_cron`, `partitioned_assets`) |
| `operators` | **task selector**: fnmatch over the operator class path; a pattern without a dot matches the class name (`BashOperator`). TaskFlow tasks are `@task` / `@task.<decorator>` (`@task.bash`), extension nodes `custom:<handler>` |
| `params` | **task selector**: conditions on task parameters (operator `params`, TaskFlow `arguments`/`decorator_kwargs`, custom `config`): `{name, present, equals, one_of, pattern}` |

With a task selector the rule applies to the matching (enabled) tasks only; without one, to
every task of the DAG (task checks) or to the DAG.

### Checks

| `type` | Holds when |
|---|---|
| `forbid` | nothing matches (the matching tasks — or the DAG — are reported) |
| `require_owner` | every selected task has an owner (own or `default_args.owner`) |
| `min_retries` (`value`) | every selected task retries at least `value` times (own or `default_args`) |
| `catchup` (`value`, default false) | the DAG's `catchup` equals `value` |
| `tags_include` (`tags`, `team`) | the DAG has all `tags` (and its team name when `team: true`) |
| `require_description` | the DAG has a description or `doc_md` |
| `max_tasks` (`value`) | at most `value` enabled tasks |
| `require_param` (`name`) | every selected task sets parameter `name` |
| `param` (`condition`) | every selected task's parameter satisfies the condition |
| `dag_field` (`field`, `present`, `equals`, `min`, `max`) | a DAG field: `description`, `doc_md`, `catchup`, `max_active_runs`, `max_active_tasks`, `dagrun_timeout_seconds`, `is_paused_upon_creation`, `fail_fast`, `max_consecutive_failed_dag_runs`, `render_template_as_native_obj`, `rerun_with_latest_version`, `timezone`, `start_date`, `end_date`, `team`, `default_args.owner`, `default_args.retries`, `default_args.retry_delay_seconds`, `default_args.execution_timeout_seconds`, `default_args.email`, `default_args.pool`, `default_args.queue` |
| `task_field` (`field`, `present`, `equals`, `min`, `max`) | a field of every selected task: `owner`, `retries`, `retry_delay_seconds`, `execution_timeout_seconds`, `pool`, `pool_slots`, `queue`, `priority_weight`, `doc_md`, `executor`, `max_active_tis_per_dagrun`, `depends_on_past`, `trigger_rule`; `owner`, `retries`, `retry_delay_seconds`, `execution_timeout_seconds`, `pool`, `queue`, `depends_on_past` fall back to `default_args` |

Rules are checked when the configuration loads: an unknown check type, a field outside the
lists above, an invalid regular expression or a duplicate id is a configuration error.

## Open Policy Agent

```yaml
policy:
  opa_url: http://opa:8181/v1/data/autoair/violations
  opa_timeout: 2          # seconds
  opa_fail_mode: open     # open | closed
  opa_token: ${OPA_TOKEN} # optional Bearer token
  opa_cache_seconds: 5    # reuse a decision for an identical input
```

AutoAir POSTs (TLS and proxy options: `outbound.clients.opa`):

```json
{"input": {
  "spec": { "...": "the DagSpec as JSON (after the environment overlay)" },
  "context": {"environment": "prod", "team": "data", "user": "alice", "action": "deploy",
              "autoair_version": "0.1.0", "airflow_version": "3.3.2"}
}}
```

`context.user`/`action` are present for deploys and deploy previews. The decision
(`result`) may be:

* `true` / `false` — allowed / one error;
* a list of messages, or of objects `{message, severity, node_id, field}`;
* an object with `deny` / `violations` (errors), `warn` / `warnings` (warnings) and/or
  `allow` (bool);
* undefined (no `result`) — nothing to report.

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

When OPA cannot be asked (timeout, connection error, HTTP ≥ 400, invalid JSON),
`opa_fail_mode: open` reports one `policy_opa` **warning** and lets the DAG pass;
`closed` reports an **error**, which blocks deploys until OPA answers again.
