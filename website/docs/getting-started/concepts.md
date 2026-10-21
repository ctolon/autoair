---
title: Concepts
description: The model behind AutoAir - the DagSpec, nodes and edges, task groups, generated code, validation, deploy targets, environments, IAM roles, collaboration and AI.
---

Each section below ends with a link to the detailed page.

## The DagSpec

A DAG built with AutoAir is a JSON document called the **DagSpec**. It is validated by a Pydantic model on the server (`autoair.spec.DagSpec`) and carries a `spec_version` (currently `3`); older documents are migrated forward when they are read, and a document newer than the running AutoAir is rejected ("spec_version N is newer than this AutoAir supports (M); upgrade AutoAir").

The spec holds:

| Part | Examples |
| --- | --- |
| DAG settings | `dag_id`, `description`, `tags`, `owner_links`, `schedule`, `start_date`, `end_date`, `catchup`, DAG timezone, `default_args`, `params`, callbacks, `max_active_runs` |
| `nodes` | operators, Python tasks, task groups, extension nodes, notes |
| `edges` | dependencies between nodes, with optional labels |
| `output_mode` | how the DAG file is produced: `python`, `loader` or `yaml` |
| `environments` | per-environment overlays (see [Environments](#environments)) |

The spec is stored as `<dag_id>.autoair.json` in the deploy target, next to the generated file, and every deploy keeps a version of it in the history (`storage.history_limit`, 100 per DAG by default). The canvas, code mode, AI and MCP tools, the importers and the REST API all read and write this one document.

You can work with specs outside the UI:

```bash
autoair generate orders_daily.autoair.json -o orders_daily.py   # spec -> Python
autoair validate orders_daily.autoair.json                     # graph, catalog, policy, DagBag import
autoair export-schema -o dag-spec.schema.json                  # JSON Schema of the spec
```

See [Spec format](/docs/extending/spec-format) and the [CLI reference](/docs/reference/cli).

## Nodes and edges

Each node has a stable internal `id` (used by edges and XCom references) and, for tasks, a `task_id` that Airflow sees. Node kinds:

| Kind | Canvas name | Becomes |
| --- | --- | --- |
| `operator` | the operator class, e.g. `BashOperator` | an operator instance; `params` hold its constructor arguments |
| `python_task` | **Python task** and its decorator variants (`@task`, `@task.branch`, `@task.short_circuit`, `@task.virtualenv`, `@task.external_python`, `@task.branch_virtualenv`, `@task.branch_external_python`, `@task.sensor`, `@task.bash`, `@task.docker`, `@task.kubernetes`, `@task.stub`, and from `apache-airflow-providers-common-ai` `@task.llm`, `@task.agent`, `@task.llm_branch` and `@task.llm_sql`) | a TaskFlow function with the body you write |
| `task_group` | **Task group** | a `TaskGroup` |
| `custom` | **Extension** | code produced by a registered node handler (see [Node SDK](/docs/extending/node-sdk)) |
| `note` | **Note** | nothing; canvas documentation only |

A parameter value can be a literal, a Jinja template, the **output of an upstream task** (an XCom reference, rendered as `t_upstream.output` for an operator, the TaskFlow call result for a Python task, optionally subscripted with a key), a Python reference (an import path), a Python function written in place, or an embedded script file. Edges become `>>` dependencies. Trigger rules, retries, setup/teardown, mapping (`expand`) and the other task attributes live on the node.

The operator palette comes from the **operator catalog**: AutoAir parses the installed Airflow providers, third-party operator packages and your custom operators statically (without importing them) and caches the result. Operators that are not installed where AutoAir runs do not appear. See [Operators and parameters](/docs/user-guide/operators-and-parameters) and [Catalog sources](/docs/extending/catalog-sources).

## Task groups

A task group is a node that contains other nodes. On the canvas you group a selection, ungroup it, or collapse it to one box. In the generated code the group becomes a `TaskGroup` and its members' task ids are prefixed with the group id (the inspector shows the effective **Airflow task id**). Edges to and from a group apply to the whole group. Groups can be nested; a group cannot be placed inside itself. Reusable groups of tasks can be saved as **fragments**. See [Canvas](/docs/user-guide/canvas) and [Templates and fragments](/docs/user-guide/templates-and-fragments).

## Generated code

The spec's `output_mode` decides what is deployed next to the spec:

| Output | Setting in **DAG settings → General → Output** | Deployed file |
| --- | --- | --- |
| `python` (default) | Generated Python file (readable, reviewable) | `<dag_id>.py`, a normal Airflow DAG using `airflow.sdk`, formatted with ruff when it is installed |
| `loader` | JSON spec + runtime loader | a small stub that builds the DAG from the spec at parse time |
| `yaml` | Airflow YAML DAG | an Airflow 3.4 YAML DAG; task groups and Python tasks are not supported |

Imports and constructs follow the **target Airflow version** (the installed one, or `target_airflow_version`); features that need a newer Airflow than the target are reported as problems. Besides the deployed output you can export a DAG as Python, Airflow YAML, dag-factory YAML or spec JSON, and import hand-written Python DAGs, Airflow YAML or DAGs that already exist in Airflow. Generated files start with a header that names the spec; AutoAir will not overwrite a generated file that was changed by hand ("ejected") unless the deploy is forced. See [Code mode](/docs/user-guide/code-mode) and [Import and export](/docs/user-guide/import-export).

## Validation

Validation runs continuously while you edit and again on every deploy:

1. **Schema**: the spec must match the model (types, required fields, identifiers).
2. **Graph**: no cycles, unique task ids, valid group membership and XCom references.
3. **Catalog**: every operator exists in the catalog and its required parameters are set with values of the right type.
4. **Jinja**: templates parse; undeclared `params`, and time variables in asset-triggered DAGs, are flagged.
5. **Policy**: organisation rules such as `require_owner`, `min_retries`, `required_tags`, `forbidden_operators`, `max_tasks` and `dag_id_prefix`, plus the policy engine (see [Policy](/docs/admin/policy); every rule is listed under [`policy`](/docs/reference/configuration#policy) in the configuration reference).
6. **Version gates**: features newer than the target Airflow.
7. **Import check**: the generated file is imported in an Airflow `DagBag` in a subprocess (warm worker pool by default). It needs Airflow installed where AutoAir runs, and on deploy it runs only for callers holding `code:execute` (`iam.import_check_requires_code_execute`).

Problems have a severity (error, warning, info); only errors block a deploy. See [Validation](/docs/user-guide/validation).

## Deploy targets

A deploy writes the spec and the generated file to a **deploy target** (`storage.mode`):

| Target | Airflow reads DAGs through | Behaviour |
| --- | --- | --- |
| `local` | a `LocalDagBundle` / the DAG folder on a shared volume | atomic file writes to `<dags_folder>/<subfolder>` (or `storage.local_path`) |
| `object` | `S3DagBundle`, `GCSDagBundle` or another object-storage bundle | writes to an fsspec URL (`storage.object_url`) |
| `git` | `GitDagBundle` or a git-sync sidecar | commits per DAG; `pr` mode opens a branch and pull request (default), `push` commits to the branch. GitHub, GitLab, Bitbucket, Azure DevOps, Gitea/Forgejo or any remote through the git CLI |

Every write uses an ETag for optimistic locking, so two people deploying the same DAG get a three-way merge instead of a lost update. Administrators can also choose the target in the UI (**Versions & Git → Deploy target…**), which lists the DAG folder, configured GitDagBundles and a git-sync checkout. See [Deploy](/docs/user-guide/deploy), [Git bundles](/docs/deployment/git-bundles) and [Object storage](/docs/deployment/object-storage).

## Environments

`environments` (for example `["dev","staging","prod"]`) lets one spec be deployed differently per environment. An **environment overlay** in the spec can override the schedule, `is_paused_upon_creation`, tags, default arguments, parameter defaults, `max_active_runs`, and map logical connection ids and Variable keys to the environment's own. With a Git target, `storage.git.environments` maps each environment to a branch and **Promote environment…** moves a DAG from one to the next. Loader-mode DAGs apply the overlay named by `AUTOAIR_ENVIRONMENT` at parse time. See [Environments](/docs/user-guide/environments).

## IAM roles

Authentication always goes through Airflow (the Airflow session in plugin mode; a verified Airflow token in standalone mode). Authorization combines Airflow's per-DAG permissions with AutoAir's own roles:

| Role | Can |
| --- | --- |
| Viewer | read specs, the catalog, code preview, history and audit |
| Author | create and edit specs, validate, test runs, use AI; proposes changes (pull requests) |
| Reviewer | read everything, approve spec versions, AI review |
| Deployer | deploy live, delete, promote between environments, trigger runs |
| CodeDeployer | Deployer, plus deploying DAGs that keep their hand-written Python file |
| Operator | trigger, backfill, clear, pause and unpause runs |
| TemplateManager | curate templates, marketplace installs and icons |
| Admin | everything, including the deploy target and configuration |

How users get these roles is set by `iam.strategies`: from Airflow's DAG and configuration permissions (default), from Airflow role names (`role_mapping`), or from AutoAir permissions created in the Airflow auth manager (`custom_view`). Four-eyes approvals (`iam.require_approval`) and direct-push rights for Git are configured here too. `autoair iam roles` prints the effective table. See [IAM](/docs/admin/iam).

## Collaboration

Several people can edit the same DAG at once. Live co-editing uses Yjs over a WebSocket at `/collab/<dag_id>`; the UI shows presence avatars, remote cursors and selections, and undo is per user. A WebSocket is opened with a short-lived ticket that requires edit permission on the DAG. It needs the `collab` extra and `features.collab`; with several replicas, rooms are shared through the database or Redis (`collab.sync`). Deploy conflicts are still caught by ETags. See [Collaboration](/docs/user-guide/collaboration).

## AI

AI assistance is optional and off until a model provider is configured (`AUTOAIR_AI__PROVIDER`, `AUTOAIR_AI__MODEL` and an API key or Airflow connection). It can generate a DAG from a prompt, edit a DAG (shown as a reviewed diff), explain problems, review a DAG and write documentation. Every AI result goes through the same validation as a human edit. Rule-based review, leak scan and optimisation work without a model. AutoAir also serves an MCP endpoint (`/mcp`) so external agents can author DAGs with the caller's permissions. See [AI introduction](/docs/ai/introduction), [Guardrails](/docs/ai/guardrails) and [MCP](/docs/ai/mcp).
