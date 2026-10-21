# Identity and access management (IAM)

Reference for contributors and operators. Task-oriented guide on the documentation site: [IAM](https://ctolon.github.io/autoair/docs/admin/iam).

AutoAir has no user database. **Airflow authenticates every user**, and Airflow's roles
decide what each user may do in AutoAir. On top of Airflow's own DAG permissions, AutoAir
has a few built-in roles (Viewer, Author, Reviewer, Deployer, CodeDeployer, Operator, TemplateManager,
Admin). Each role is a set of fine-grained AutoAir permissions such as `spec:write`,
`deploy` or `deploy:direct_push`. Admins assign those roles in Airflow. Settings are in the
`iam` section (`autoair.iam.config.IamSettings`, [§ 9](#9-settings-reference)).

With no IAM configuration, every signed-in user gets the roles Airflow's standard
permissions imply (see `airflow_permissions` below); only pushing straight past a pull
request is limited to Airflow admins.

### Switching IAM off (`AUTOAIR_FEATURES__IAM=false`)

AutoAir's own authorization layer can be turned off completely (`features.iam`, default on).
AutoAir then works as follows:

* every signed-in user gets the **`Authenticated` baseline** plus the roles Airflow's standard
  permissions grant (the built-in `airflow_permissions` defaults: DAG read → Viewer, DAG edit →
  Author/Reviewer/Deployer/Operator/TemplateManager, configuration edit → Admin). In standalone
  mode, where AutoAir cannot ask Airflow about DAG permissions, that is every role — Airflow
  still enforces its RBAC on every proxied call;
* `iam.strategies`, `iam.role_mapping`, `iam.role_prefix`, `iam.roles`, `iam.default_roles`
  and custom views are ignored;
* no four-eyes approvals (`iam.require_approval` is ignored: deploys go live directly);
* no direct-push roles and no forced pull requests: **the git target's mode decides** (PR mode
  always opens a pull request, push mode always pushes; `direct_push=true` is refused);
* sign-in (`iam.login_*`), denial auditing and `iam.anonymous_roles` (`--no-auth`) keep their
  configured values; MCP tools follow the same rules.

`GET /api/me` still answers, with `"iam_enabled": false` and `strategies:
["airflow_permissions"]`, so the UI shows what the user may do.

## 1. Authentication

| | Plugin mode (inside Airflow's API server) | Standalone (`autoair serve`) |
|---|---|---|
| Who signs the user in | Airflow (its login page, `_token` cookie or `Authorization: Bearer`) | Airflow: the browser holds an **Airflow** access token |
| How AutoAir checks it | Airflow's `get_user` dependency on every route (auth manager decodes and validates the JWT) | `TokenVerifier`: `GET {airflow}/api/v2/dags?limit=1` with the token, cached for `AUTOAIR_AUTH_CACHE_TTL` s (never past the token's `exp`) |
| How the user gets a token | Airflow's login | **Username/password form** (`POST /api/auth/login`, see §5), or pasting a token (`POST {airflow}/auth/token`) |
| Proxied Airflow calls | The caller's cookie/bearer is forwarded, so Airflow enforces its RBAC | Same |
| `--no-auth` | n/a | No authentication. The user gets `iam.anonymous_roles` (default `Admin`) but never direct push. Development only. |

Expired tokens: standalone rejects a token whose `exp` has passed without asking Airflow
(`401 Airflow token expired`). The UI then shows the sign-in form again.

## 2. Authorization model

### Permissions

| Permission | Allows | Airflow custom view (`custom_view` strategy) |
|---|---|---|
| `builder:use` | Operator catalog, code preview, validation, diff/merge, templates, icons, YAML/JSON import, LSP | `can_read` on `AutoAir Builder` |
| `spec:read` | List/read stored specs, history, versions, VCS status, reviews | `can_read` on `AutoAir Specs` |
| `audit:read` | A DAG's audit trail | `can_read` on `AutoAir Audit Log` |
| `airflow:read` | Read Airflow objects through AutoAir: runs, logs, XCom, pools, connections, variables, assets, import errors, status, next-runs preview | `can_read` on `AutoAir Airflow Resources` |
| `connection:write` | Create and test Airflow connections | `can_create` on `AutoAir Airflow Resources` |
| `spec:write` | Save a spec. In git PR mode a save opens or updates a pull request (a proposal). Also joins live co-editing. | `can_edit` on `AutoAir Specs` |
| `spec:delete` | Delete a spec and its DAG files | `can_delete` on `AutoAir Specs` |
| `code:execute` | Run DAG code on the AutoAir server: import check (`/validate?import_check=true`), Python/Airflow DAG import | `can_create` on `AutoAir Code Execution` |
| `code:raw` | Save raw code DAGs: hand-written DAG files deployed as written (arbitrary Python); also needs `features.code_editor` and `features.python_code` | `can_edit` on `AutoAir Raw Code` |
| `run:test` | Test a task or DAG from the builder | `can_create` on `AutoAir Test Runs` |
| `catalog:refresh` | Rebuild the operator catalog | `can_create` on `AutoAir Catalog` |
| `ai:use` | All AI assistant routes | `can_create` on `AutoAir AI` |
| `review` | Approve, request changes on, or comment a spec version | `can_create` on `AutoAir Reviews` |
| `deploy` | Saves that go live: local and object targets, git push mode, direct pushes | `can_create` on `AutoAir Deploy` |
| `deploy:direct_push` | Git PR mode: `direct_push: true` pushes straight to the branch | `can_create` on `AutoAir Direct Push` |
| `deploy:overwrite_ejected` | Overwrite a DAG file AutoAir would otherwise not replace, after an explicit confirmation: a generated file changed outside AutoAir (also needs `preserve.ejected_files=overwrite`), the hand-written file AutoAir keeps for a DAG (switch to generate), or the file of a `force=true` deploy of another output mode or raw code. Every overwrite also needs `preserve.allow_force_overwrite=true` (default `false`; without it nobody can overwrite, administrators included). Deleting a DAG whose hand-written file AutoAir keeps needs only this permission. Files without AutoAir's marker are never overwritten by `force`. See [Editing existing Python DAGs](https://ctolon.github.io/autoair/docs/user-guide/editing-python-dags#overwriting-is-off-by-default). Never granted by a wildcard | `can_create` on `AutoAir Overwrite Ejected Files` |
| `promote` | Promote a DAG between environments | `can_create` on `AutoAir Promote` |
| `run:trigger` | Trigger runs, create asset events | `can_create` on `AutoAir DAG Runs` |
| `run:manage` | Pause/unpause, clear, backfill | `can_edit` on `AutoAir DAG Runs` |
| `template:write` | Save/delete templates, install marketplace entries | `can_create` on `AutoAir Templates` |
| `icon:write` | Upload/delete icons, set default icons | `can_create` on `AutoAir Icons` |
| `admin:deploy_target` | Change, test or reset the server-wide deploy target | `can_edit` on `AutoAir Deploy Target` |
| `admin:config` | Read and change AutoAir's runtime configuration (`/api/config`) | `can_edit` on `AutoAir Configuration` |
| `admin:panel` | Open the admin panel (`/api/admin/*`): audit browser, users, AI usage, database and caches; changes there also need their own permission | `can_read` on `AutoAir Admin Panel` |
| `ai:admin` | Administer AI: knowledge base, prompt overrides, AI audit log, privileged AI tools; also the AI role used by a separate AI service | `can_edit` on `AutoAir AI Settings` |

### Built-in roles

| Role | Permissions |
|---|---|
| **Viewer** | `builder:use`, `spec:read`, `audit:read`, `airflow:read` |
| **Author** | Viewer + `spec:write`, `code:execute`, `code:raw`, `run:test`, `ai:use`, `connection:write`, `catalog:refresh`. Authors *propose*: in git PR mode their saves open pull requests. On local/object targets, and in push mode, they cannot go live without `deploy`. |
| **Reviewer** | Viewer + `review`, `ai:use` |
| **Deployer** | Viewer + `spec:write`, `spec:delete`, `deploy`, `promote`, `run:trigger` |
| **CodeDeployer** | Deployer + `code:execute`, `code:raw`: deploys DAGs that keep their hand-written Python file (imported or edited in the code mode) |
| **Operator** | Viewer + `run:trigger`, `run:manage` |
| **TemplateManager** | Viewer + `template:write`, `icon:write` |
| **Admin** | Every permission except `deploy:direct_push` (`deploy:overwrite_ejected` included) |
| *Authenticated* | Baseline of the `airflow_permissions` strategy: `builder:use`, `airflow:read`, `ai:use`, `connection:write`, `run:trigger`, `run:manage`. Airflow still enforces its own RBAC on every proxied call. |

Deploying a DAG that keeps its hand-written Python file (imported, or applied in the code mode; source policy preserve) runs a round-trip import of the file, which needs `code:execute`, and writing the imported file itself needs `code:raw`. **Deployer** has neither, deliberately (the kept file is arbitrary Python): such a deploy is refused with a message naming the missing permission. Give those users the **CodeDeployer** role (Deployer + `code:execute` + `code:raw`), or deploy the DAG with the source policy *Generate*.

`deploy:direct_push` is in **no** role definition. It goes to the roles in
`AUTOAIR_IAM__DIRECT_PUSH_ROLES` (default `["Admin"]`). A deploy target can override that
list with `AUTOAIR_STORAGE__GIT__DIRECT_PUSH_ROLES` (or the same field in
`team_storage`): unset (the default) means `iam.direct_push_roles` applies, `[]` means
nobody. Set `iam.direct_push_roles` to `[]` to forbid direct pushes everywhere (except for
users granted the permission explicitly, see below).

`deploy:direct_push` only ever comes from a **trusted** grant: a role from a verified
Airflow check or a verified role claim, an explicit `"deploy:direct_push"` in a custom role
held through such a grant, or the Airflow custom view. It is never granted by a wildcard
such as `"*"`, `"deploy*"` or `"deploy:*"`, never through unverified roles (standalone `airflow_permissions`),
and never to anonymous users (`--no-auth`).

Custom roles, or overrides of built-in ones, go in `AUTOAIR_IAM__ROLES`. Wildcards are
allowed:

```bash
AUTOAIR_IAM__ROLES='{"Auditor": ["spec:read", "audit:*"], "Pusher": ["spec:*", "deploy", "deploy:direct_push"]}'
AUTOAIR_IAM__DEFAULT_ROLES='["Viewer"]'   # every signed-in user
```

`autoair iam roles [--json]` prints the effective table.

The settings are checked at startup: an unknown permission in `iam.roles` (a typo, or a
wildcard that matches nothing) is an error; an unknown role name in `default_roles`,
`anonymous_roles`, `direct_push_roles`, `role_mapping` or `airflow_permission_roles` is
logged as a warning (it grants nothing).

### Per-DAG access stays with Airflow

Routes about one DAG (`/specs/{dag_id}`, `/vcs/{dag_id}/*`, `/collab/{dag_id}/*`) still
need Airflow's DAG access for that DAG: `requires_access_dag("GET"|"PUT")` with the
`dag_id`, honouring Airflow's per-DAG permissions and multi-team. The AutoAir permission is
checked **in addition**. A Deployer still cannot deploy a DAG they cannot edit in Airflow.
In standalone mode Airflow enforces DAG access only on the calls AutoAir proxies.

## 3. Mapping strategies: managing AutoAir access in Airflow

`AUTOAIR_IAM__STRATEGIES` is a list. The user's permissions are the **union** of what each
listed strategy grants, plus `default_roles`.

### `airflow_permissions` (default)

AutoAir roles are derived from Airflow's standard permissions, evaluated for the request's
`dag_id` when it has one:

| Airflow permission | Setting | Default AutoAir roles |
|---|---|---|
| signed in | `AUTOAIR_IAM__AIRFLOW_PERMISSION_ROLES__AUTHENTICATED` | Authenticated |
| DAG read | `AUTOAIR_IAM__AIRFLOW_PERMISSION_ROLES__DAG_READ` | Viewer |
| DAG edit | `AUTOAIR_IAM__AIRFLOW_PERMISSION_ROLES__DAG_EDIT` | Author, Reviewer, Deployer, Operator, TemplateManager |
| Configuration edit | `AUTOAIR_IAM__AIRFLOW_PERMISSION_ROLES__CONFIGURATION_EDIT` | Admin |

In standalone mode AutoAir cannot ask Airflow these questions. Every signed-in user gets
all the roles above, **including Admin**. Those roles are *unverified*, so
they never unlock direct push. To get real roles in standalone mode, use `role_mapping`
**alone**: combined with `airflow_permissions` (e.g. `["airflow_permissions",
"role_mapping"]`), the union still gives every user every role, so the mapping restricts
nothing. AutoAir logs a warning at startup for that combination in standalone mode.

### `role_mapping`: Airflow role names to AutoAir roles

Where the user's Airflow roles come from:

* **FAB** (plugin mode): `user.roles` (role names, including roles from groups resolved at login).
* **SimpleAuthManager** (plugin mode): `user.role` (`ADMIN`, `OP`, `USER`, `VIEWER`).
* **Standalone**: the token's claims named in `AUTOAIR_IAM__ROLES_CLAIMS` (default `role`,
  `roles`). SimpleAuthManager tokens carry `role`. These claims are trusted only because
  Airflow verified the token. FAB tokens carry only `sub`, so for FAB use plugin mode or
  `custom_view`.

How names are mapped (case-insensitive):

* `AUTOAIR_IAM__ROLE_MAPPING`. The default maps `Admin` to Admin; `Op` to Author,
  Reviewer, Deployer, Operator and TemplateManager; `User` to Author and Operator; and
  `Viewer` to Viewer. Example: `{"data-eng": ["Author", "Deployer"]}`.
* `AUTOAIR_IAM__ROLE_PREFIX` (default `"AutoAir "`): an Airflow role named
  `AutoAir Deployer` grants Deployer. In FAB, create these roles (empty, or with
  `autoair iam sync-fab --create-roles`) and assign them to users or groups in
  *Security → List Users*.

### `custom_view`: AutoAir permissions as Airflow permissions

Each AutoAir permission is checked with the auth manager's
`is_authorized_custom_view(method, "AutoAir <Resource>")` (see the permission table). Admins
grant them directly, for example "can_create on AutoAir Direct Push" to the `Release
Managers` role. This works in plugin mode only. The answers are cached per user and token for
`AUTOAIR_IAM__CUSTOM_VIEW_CACHE_SECONDS` (30 s; `0` disables the cache), so a request does
not cost ~20 synchronous auth-manager checks. A permission revoked in Airflow therefore
takes up to that long to apply.

| Auth manager | `is_authorized_custom_view` | User roles exposed | Recommended strategy |
|---|---|---|---|
| **FAB** (`apache-airflow-providers-fab`) | Real check: `(can_<action>, resource)` in the user's permissions. Permissions must exist in FAB's DB: run `autoair iam sync-fab [--create-roles]` once. | `user.roles` | `custom_view` (fine-grained in the Airflow UI) or `role_mapping` |
| **SimpleAuthManager** (Airflow default) | **Grants every custom view to every user with any role**, whatever the method. AutoAir therefore ignores `custom_view` with it and logs a warning. | `user.role`, also in the JWT | `role_mapping` |
| **Keycloak** (`apache-airflow-providers-keycloak`) | Real check: resource type `Custom`, resource id `<custom_view_prefix> <resource>` (`AutoAir Builder`, `AutoAir Specs`, `AutoAir Audit Log`, `AutoAir Airflow Resources`, `AutoAir Code Execution`, `AutoAir Test Runs`, `AutoAir Catalog`, `AutoAir AI`, `AutoAir Reviews`, `AutoAir Deploy`, `AutoAir Direct Push`, `AutoAir Promote`, `AutoAir DAG Runs`, `AutoAir Templates`, `AutoAir Icons`, `AutoAir Deploy Target`, `AutoAir Configuration`, `AutoAir Admin Panel`, `AutoAir AI Settings`), scope = method. Define them in Keycloak authorization. | not exposed | `custom_view` |
| **AWS** (`apache-airflow-providers-amazon`) | Real check: Verified Permissions entity `Custom::<custom_view_prefix> <resource>` (the resources listed for Keycloak, such as `Custom::AutoAir Deploy`), action = method | not exposed | `custom_view` |
| Custom auth managers | Whatever they implement | `user.roles` / `user.role` when present | either |

FAB example:

```bash
airflow fab-db migrate        # once, if FAB is new
autoair iam sync-fab --create-roles
# Airflow UI → Security → List Roles: "AutoAir Deployer" holds "can create on AutoAir Deploy" and the other Deployer permissions
AUTOAIR_IAM__STRATEGIES='["custom_view"]'            # or '["role_mapping"]', or both
```

`sync-fab --create-roles` refuses to run with an empty `iam.role_prefix`: the roles would be
named `Viewer`, `Author`, `Reviewer`, `Deployer`, `Operator`, `TemplateManager` and `Admin`, and it would modify Airflow's own built-in roles.

Strategies combine. For example, in plugin mode `["airflow_permissions", "role_mapping"]`
keeps the default roles for everybody and adds Reviewer to users with the `AutoAir
Reviewer` role (in standalone mode see the warning above). To
*restrict* who can deploy, leave out `airflow_permissions`: with it, every DAG editor is a
Deployer.

## 4. Pull request or direct push

The git deploy mode is set per target with `AUTOAIR_STORAGE__GIT__MODE` (`pr` or `push`).
The deploy request (`PUT /api/specs/{dag_id}`) takes `direct_push: true | false | null`.

| Target mode | `direct_push` | Result | Permissions needed |
|---|---|---|---|
| `pr` | `null` (default) | branch `autoair/<dag_id>` + pull request | `spec:write` |
| `pr` | `true` | commit to the target branch | `spec:write`, `deploy`, `deploy:direct_push` (otherwise **403**) |
| `push` | `null` | commit to the branch; or a PR if `AUTOAIR_IAM__FORCE_PR_WITHOUT_DIRECT_PUSH=true` and the user lacks `deploy:direct_push` | `spec:write` + `deploy` when it goes live |
| `push` | `false` | pull request (always allowed) | `spec:write` |
| `push` | `true` | commit to the branch | `deploy` + `deploy:direct_push` |
| local / object targets | any | written live | `spec:write`, `deploy` (`direct_push: true` still needs `deploy:direct_push`) |

The flag cannot raise anyone's privileges: `direct_push: true` without the permission is
refused (403, audited), never silently downgraded. A direct push in PR mode accepts the
`base_etag` of the open pull request's version, so an admin can push the proposal as it is.

### Four-eyes approvals and deploy proposals

With `AUTOAIR_IAM__REQUIRE_APPROVAL=true`, a deploy that **goes live** needs an approval of
that exact spec version (its etag) by **another** user holding `review`. On local, object
and git push targets saving *is* deploying, so an unapproved deploy becomes a **deploy
proposal** instead of being written:

1. `PUT /api/specs/{dag_id}` (needs `spec:write`; an Author may propose without `deploy`)
   validates the spec, stores it as a proposal and answers **202**:
   `{"deployed": false, "status": "pending_approval", "proposal_etag": "<etag>", "report": {…}, "message": "…"}`.
   Nothing is written. Proposing the same version again keeps the first proposal (and its
   author).
2. Reviewers find it with `GET /api/specs/{dag_id}/proposals` (needs `spec:read`) and read
   it with `GET /api/specs/{dag_id}/proposals/{etag}`: the proposed `spec`, the stored spec
   now (`current_spec`/`current_etag`, the diff base), `stale` (the stored spec changed since
   the author's `base_etag`), `reviews`, `approved_by`, `changes_requested_by`, `approved`.
3. `POST /api/specs/{dag_id}/reviews` with `{"etag": "<proposal etag>", "verdict":
   "approve" | "request_changes" | "comment", "comment": "…"}` (needs `review`). The
   proposal's author cannot approve it (403).
4. `POST /api/specs/{dag_id}/proposals/{etag}/deploy` (needs `deploy`; optional body
   `{"force": false, "message": "…"}`) deploys it once another user than its author
   approved it. Anyone holding `deploy` may, **including the author**. The proposal is then
   removed and the `deploy` audit record carries `approved_by` and `proposed_by`.

A direct `PUT` of a version that already has an approval (by someone other than the
deployer, or than the author of the pending proposal of that version) deploys immediately
(`deploy` needed). Each reviewer's latest verdict counts, so a later
`request_changes` withdraws an approval. Reviews of stored versions (not proposals) are
accepted too; self-approvals of those do not count.

Proposals and reviews live in their own small store (`AUTOAIR_IAM__PROPOSALS_URL`, any
fsspec URL, default `<state>/proposals`; use shared storage with several replicas), not in
the audit trail, so checking an approval never scans the audit log (reviews are still
audited). They expire after `AUTOAIR_IAM__PROPOSAL_TTL_SECONDS` (7 days); a deployed
proposal is removed at once.

Pull requests are exempt: on git targets in PR mode the pull request *is* the proposal and
its review happens in the git host. A direct push (`direct_push: true`) goes live, so it
follows the rule above (a proposal deployed later keeps its `direct_push` request, and the
deploying user then needs `deploy:direct_push`). With `REQUIRE_APPROVAL=false` nothing of
this applies.

### Import check on deploy and `code:execute`

`import_check_on_deploy` (default on) imports the generated DAG file with a DagBag in a
sandboxed subprocess on the AutoAir server: that **executes DAG code**, including inline
Python callables and decorators. With `AUTOAIR_IAM__IMPORT_CHECK_REQUIRES_CODE_EXECUTE=true`
(the default) a deploy runs it only when the deploying user holds `code:execute`; for
others it is skipped and the report carries an info problem
(`code: import_check_skipped`, "import check skipped: …"). The deploy is still validated
statically (spec, policy, code generation).

The trade-off: a Deployer without `code:execute` cannot run code on the AutoAir server,
but their deploy may then ship a DAG that fails to import in Airflow (Airflow shows the
import error). Give deployers `code:execute` (e.g. the Author role too) to keep the
pre-deploy import check, or set the setting to `false` to always run it (any user with
`deploy` can then execute code on the AutoAir server through the sandbox).

## 5. Standalone sign-in with username and password

`POST /api/auth/login` takes `{"username": "...", "password": "..."}` and returns
`{"access_token", "token_type": "bearer", "user", "expires_at"}`.

* AutoAir forwards the credentials once to `POST {airflow_url}/auth/token` (SimpleAuthManager,
  FAB and Keycloak all serve it) and returns Airflow's JWT. The browser stores it in
  `sessionStorage`, exactly like a pasted token.
* The password is never stored or logged (`SecretStr`, no request logging), and the
  response has `Cache-Control: no-store`.
* Failed attempts (Airflow answers 400/401/403) are counted per client IP and per username
  (hashed) in memory. After `AUTOAIR_IAM__LOGIN_MAX_FAILURES` (5) within
  `AUTOAIR_IAM__LOGIN_WINDOW_SECONDS` (300), further attempts get **429** with
  `Retry-After`, and Airflow is not asked. Each attempt is reserved before Airflow is asked,
  so concurrent requests cannot exceed the limit; it is refunded when the sign-in succeeds
  or Airflow cannot be reached. A successful sign-in clears the username's counter. At most
  `AUTOAIR_IAM__LOGIN_MAX_TRACKED_KEYS` (10000) IPs/usernames are tracked (least recently
  used first forgotten). The counters are per process: put a shared rate limiter in front
  of several replicas.
* `POST /api/auth/logout` drops the token from AutoAir's verification cache and asks
  Airflow to revoke it (`GET /api/v2/auth/logout`, Airflow 3.2 and later). It reports
  `revoked`, true only when Airflow answered 2xx (a redirect to a login page is not a
  revocation).
* Disable it with `AUTOAIR_IAM__LOGIN_ENABLED=false`. In plugin mode the route does not
  exist: Airflow's own login is used.
* Behind a reverse proxy, run uvicorn with `--proxy-headers`/`--forwarded-allow-ips` so the
  client IP is the real one.

## 6. What the UI asks: `GET /api/me`

`GET /api/me` (also embedded as `iam` in `GET /api/meta`). The deploy rights are those on
the deploy target of this deployment's team (`AUTOAIR_TEAM`), or of `?team=<name>`:

```json
{
  "user": "alice",
  "roles": ["Author"],
  "permissions": ["ai:use", "airflow:read", "audit:read", "builder:use", "catalog:refresh",
                  "code:execute", "code:raw", "connection:write", "run:test", "spec:read", "spec:write"],
  "auth": "bearer",
  "mode": "standalone",
  "strategies": ["role_mapping"],
  "login_enabled": true,
  "deploy": {
    "git_mode": "pr",
    "can_save": true,
    "can_deploy_live": false,
    "direct_push": false,
    "default_opens_pr": true,
    "require_approval": false
  }
}
```

The frontend hides or disables actions the user lacks: Deploy/Save (`spec:write`, or
`deploy` when `default_opens_pr` is false), the "push directly" option
(`deploy.direct_push`), Delete (`spec:delete`), Promote (`promote`), Trigger/Backfill/
Clear/Pause (`run:trigger` / `run:manage`), Test runs (`run:test`), import check and Python
import (`code:execute`), AI panel (`ai:use`), template save and marketplace install
(`template:write`), icon upload (`icon:write`), the deploy-target and configuration settings
(`admin:deploy_target`, `admin:config`), the audit tab (`audit:read`) and Approve
(`review`). The server enforces all of these whatever the UI shows.

## 7. Routes and permissions

| Route(s) | Permission(s) | Plus Airflow per-DAG check |
|---|---|---|
| `GET /meta`, `GET /me` | signed in | |
| `GET /health`, `/ready`, `/metrics`, `GET /icons/{id}` (files) | none | |
| `POST /auth/login`, `POST /auth/logout` (standalone) | none (rate-limited) | |
| `/catalog/*` (GET) | `builder:use` | |
| `POST /catalog/refresh` | `catalog:refresh` | |
| `GET /airflow/*`, `POST /airflow/next-runs` | `airflow:read` | Airflow enforces |
| `POST /airflow/connections`, `/airflow/connections/test` | `airflow:read` + `connection:write` | Airflow enforces |
| `POST /airflow/dags/{id}/trigger`, `/airflow/assets/{id}/events` | `airflow:read` + `run:trigger` | Airflow enforces |
| `PUT /airflow/dags/{id}/paused`, `PUT /airflow/dags/{id}/scheduling-state`, `POST /airflow/dags/{id}/runs/{run_id}/clear`, `POST /airflow/dags/{id}/backfill` | `airflow:read` + `run:manage` | Airflow enforces |
| `/ai/*` | `ai:use` | |
| `/lsp/*`, `/codegen`, `/validate`, `/export/dag-factory`, `/schema/spec`, `/interchange/*` | `builder:use` | |
| `/validate?import_check=true`, `POST /interchange/python/import`, `POST /interchange/airflow/{id}/import` | + `code:execute` | |
| `GET /templates*` / `POST`, `DELETE /templates*` | `builder:use` / `template:write` | |
| `GET /icons`, `GET /icons/assignments` / `POST /icons`, `DELETE /icons/{id}`, `PUT /icons/assignments` | `builder:use` / `icon:write` | |
| `GET /specs`, `GET /specs/{id}`, `GET /specs/{id}/reviews` | `spec:read` | DAG read |
| `GET /specs/{id}/proposals`, `GET /specs/{id}/proposals/{etag}` | `spec:read` | DAG read |
| `POST /specs/{id}/proposals/{etag}/deploy` | `deploy` (+ an approval by another user than the author, + `deploy:direct_push` if the proposal asked for it) | DAG edit |
| `GET /specs/{id}/audit` | `audit:read` | DAG read |
| `POST /specs/{id}/reviews` | `review` | DAG read |
| `PUT /specs/{id}` | `spec:write` (+ `deploy` if live, + `deploy:direct_push` for `direct_push: true`; with approvals required an unapproved live deploy becomes a proposal, 202) | DAG edit |
| `PUT /specs/{id}`, `POST /specs/{id}/deploy-preview` with a raw code DAG (`raw_code` set) | + `code:raw`; 404 while `features.code_editor` or `features.python_code` is off | DAG edit |
| `DELETE /specs/{id}` | `spec:delete` | DAG edit |
| `/vcs/*` (reads, diff, merge) | `spec:read` | DAG read |
| `POST /vcs/{id}/promote` | `promote` | DAG edit |
| `POST /runner/test-task`, `/runner/test-dag` | `run:test` | |
| `POST /collab/{id}/ticket` | `spec:write` | DAG edit |
| `GET /deploy-target` / `PUT`, `POST /test`, `DELETE` | `builder:use` / `admin:deploy_target` | |
| `GET /marketplace` / `POST /marketplace/install` | `builder:use` / `template:write` | |
| `/config` | `admin:config` | |
| `/admin/*` | `admin:panel` (changes also need their own permission, e.g. `admin:config` for MCP servers) | |

A test (`tests/integration/test_iam_api.py::test_every_protected_route_requires_a_permission`)
fails when a new route has no permission.

## 8. Audit

Denials (403) are recorded in the audit trail as `permission_denied`, with the user,
permission, method, path, roles and the `dag_id` when there is one. They then also show in
that DAG's audit. Turn this off with `AUTOAIR_IAM__AUDIT_DENIALS=false`. Deploys record
`direct_push` (and `approved_by`/`proposed_by` with approvals), proposals are `propose`
records and reviews are `review` records. Deploys, deletes, promotions, proposals and reviews
also carry the full deploy context (user name and e-mail, AutoAir version, environment, spec
etag before/after, validation summary, approvals/reviewers, the link to the DAG, how it was
written — `write`: `deploy`, `pull_request` or `direct_push` — and which credentials were used);
see [configuration § deploy governance](configuration.md#deploy-governance).

### Audit sinks

Airflow 3's REST API cannot write event-log entries: `/api/v2/eventLogs` only offers
`GET /eventLogs` and `GET /eventLogs/{event_log_id}` (checked against Airflow 3.0–3.3; event logs
are written by Airflow itself for its own actions). AutoAir therefore keeps its own audit trail
and emits every record to the sinks of `audit.sinks` (`AUTOAIR_AUDIT__SINKS`):

| Sink | Where |
|---|---|
| `file` | JSON lines `<audit_url>/<day>/<host>-<pid>.jsonl` (fsspec URL; default `<state>/audit`) |
| `db` | the `<prefix>audit` table of the SQL database (`database.backend` sqlite/postgresql) |
| `log` | one line on the `autoair.audit` logger: `audit {json}`, the record also as the `audit` attribute of the log record for JSON formatters / log shippers |
| `otel` | one OpenTelemetry span `audit <action>` with `autoair.audit.{action,user,dag_id,timestamp,details}` attributes, exported by the configured tracer provider (`pip install 'autoair[otel]'`, `OTEL_*` variables) |

Unset (the default) means the state store (`db` with a database, else `file`) plus `log`.
The audit view of the UI and `/api/admin/audit` read the
database when `db` is a sink, else the files; with only `log`/`otel` the in-app audit view is
empty. Example: `AUTOAIR_AUDIT__SINKS='["db","log","otel"]'`.

### Git tokens typed per deploy: security notes

`storage.git.request_token` lets a user deploy, delete, promote or open a pull request with a
git token typed in the dialog ([configuration](configuration.md#git-tokens-typed-per-deploy-storagegitrequest_token)).
AutoAir never stores git secrets, and this does not change that:

* **IAM still decides.** The token only changes *which git credentials* a write uses: the
  caller still needs `spec:write`, `deploy`, `deploy:direct_push`, `spec:delete`… and the
  approval rules. A token can never grant an AutoAir permission.
* **Transport.** Header `X-AutoAir-Git-Token` only (never URL, query or body); accepted over
  HTTPS or from a loopback client (`server.proxy_headers` / `forwarded_allow_ips` decide what
  the scheme and client are behind a proxy), refused otherwise (403) unless
  `storage.git.request_token_allow_insecure`. Only sent to `https://` git hosts; SSH remotes
  are refused. Do not let proxies or OpenTelemetry instrumentations capture request headers
  (`OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_REQUEST`) for AutoAir's routes.
* **Lifetime.** The route wraps it in an object whose `repr` is masked and that cannot be
  pickled, passes it explicitly to a throw-away git target built for that request (no
  cache, no TTL re-read, no global state) and drops it with the request. The local git
  provider sends it as `http.extraHeader` through `GIT_CONFIG_*` environment variables, with
  credential helpers disabled: nothing reaches argv, `.git/config`, the remote URL or a
  credential store.
* **Never echoed.** While the request runs the token (plus its Basic and URL encodings) is
  registered with `autoair.secret_mask`, which masks it in every log record (message,
  arguments, traceback), every audit record and every error detail; provider errors also
  redact it at the source. Unexpected failures of a write made with a token become a masked
  502 without the original exception attached. Commit messages, PR bodies and the deploy
  context never contain it. Tests grep captured logs, audit files, responses and the state
  folder for it (`tests/integration/test_request_token_api.py`).
* **Refusal is final.** A token the host rejects is a 401/403 (`git_token_rejected`); AutoAir
  does not retry with the configured credentials.
* **Forced connection wins** unless `request_token_overrides_forced=true`.
* **Attribution.** Audit records carry `credential: request-token` and, best effort, the
  host's login of the token owner (`credential_user`).
* **MCP.** Off by default (`storage.git.mcp_request_token`); when on, only as the header of an
  MCP HTTP request, never as a tool argument (which would reach the model's context and tool
  logs). The MCP IAM middleware (`autoair/mcp/iam.py`) refuses the header when the flag is
  off, refuses MCP writes without it when the target requires a token, and masks it while
  the tool runs.
* **Browser.** The UI keeps it in the dialog's React state only — no zustand store, draft,
  local/session storage, URL, React Query key or mutation variables — clears it after every
  attempt and when the dialog closes; the field has `autocomplete="off"`, no spellcheck and
  asks password managers not to save it.

## 9. Settings reference

Types and full descriptions: [configuration reference § `iam`](configuration-reference.md#iam) and [§ `iam.airflow_permission_roles`](configuration-reference.md#iamairflow_permission_roles).

| Setting | Default | Description |
|---|---|---|
| `AUTOAIR_IAM__STRATEGIES` | `["airflow_permissions"]` | `airflow_permissions`, `role_mapping`, `custom_view` (union) |
| `AUTOAIR_IAM__AIRFLOW_PERMISSION_ROLES__AUTHENTICATED` | `["Authenticated"]` | Roles granted by signing in (§3) |
| `AUTOAIR_IAM__AIRFLOW_PERMISSION_ROLES__DAG_READ` | `["Viewer"]` | Roles granted by DAG read (§3) |
| `AUTOAIR_IAM__AIRFLOW_PERMISSION_ROLES__DAG_EDIT` | `["Author", "Reviewer", "Deployer", "Operator", "TemplateManager"]` | Roles granted by DAG edit (§3) |
| `AUTOAIR_IAM__AIRFLOW_PERMISSION_ROLES__CONFIGURATION_EDIT` | `["Admin"]` | Roles granted by configuration edit (§3) |
| `AUTOAIR_IAM__ROLE_MAPPING` | `{"Admin": ["Admin"], "Op": ["Author", "Reviewer", "Deployer", "Operator", "TemplateManager"], "User": ["Author", "Operator"], "Viewer": ["Viewer"]}` | Airflow role → AutoAir roles |
| `AUTOAIR_IAM__ROLE_PREFIX` | `"AutoAir "` | `AutoAir <Role>` Airflow roles grant `<Role>` |
| `AUTOAIR_IAM__ROLES_CLAIMS` | `["role", "roles"]` | Standalone: JWT claims with role names |
| `AUTOAIR_IAM__ROLES` | `{}` | Custom/overridden roles (wildcards) |
| `AUTOAIR_IAM__DEFAULT_ROLES` | `[]` | Roles of every signed-in user |
| `AUTOAIR_IAM__ANONYMOUS_ROLES` | `["Admin"]` | Roles with `--no-auth` (never direct push) |
| `AUTOAIR_IAM__CUSTOM_VIEW_PREFIX` | `AutoAir` | Airflow resource name prefix |
| `AUTOAIR_IAM__DIRECT_PUSH_ROLES` | `["Admin"]` | Roles with `deploy:direct_push` (a target's `storage.git.direct_push_roles` overrides) |
| `AUTOAIR_IAM__FORCE_PR_WITHOUT_DIRECT_PUSH` | `false` | Push mode: open PRs for users without `deploy:direct_push` |
| `AUTOAIR_IAM__REQUIRE_APPROVAL` | `false` | Four-eyes rule for live deploys (deploy proposals, §4) |
| `AUTOAIR_IAM__PROPOSALS_URL` | `<state>/proposals` | Store of deploy proposals and reviews (fsspec URL) |
| `AUTOAIR_IAM__PROPOSAL_TTL_SECONDS` | `604800` | Proposals and reviews expire after this |
| `AUTOAIR_IAM__IMPORT_CHECK_REQUIRES_CODE_EXECUTE` | `true` | Deploy import check only for users with `code:execute` |
| `AUTOAIR_IAM__CUSTOM_VIEW_CACHE_SECONDS` | `30` | `custom_view` answers cached per user and token |
| `AUTOAIR_IAM__AUDIT_DENIALS` | `true` | Audit 403s |
| `AUTOAIR_IAM__LOGIN_ENABLED` | `true` | Standalone password sign-in |
| `AUTOAIR_IAM__LOGIN_MAX_FAILURES` / `AUTOAIR_IAM__LOGIN_WINDOW_SECONDS` | `5` / `300` | Sign-in rate limit |
| `AUTOAIR_IAM__LOGIN_MAX_TRACKED_KEYS` | `10000` | Rate limiter memory cap (IPs + usernames) |
| `AUTOAIR_IAM__LOGIN_TIMEOUT` | `10` | Timeout (s) of Airflow's token call |

## 10. Limitations

* MCP tools do not go through the REST routes. Over HTTP each tool checks the same
  permission as its REST counterpart (`autoair.mcp.iam`) and tools the caller may not use are
  hidden; over stdio no AutoAir principal applies (the server runs as the local user). See
  [mcp.md](mcp.md) and `AUTOAIR_AI__MCP__ALLOW_WRITES`.
* Standalone mode cannot evaluate Airflow's per-DAG or configuration permissions locally.
  Use `role_mapping` with token claims, or plugin mode, for real governance.
* The sign-in rate limiter is in memory, per process.

## Separate AI service

When the AI backend runs as its own service (`autoair ai-serve`, `ai.service_url`), the main
server authorises the caller (`ai:use`) and forwards an HMAC-signed identity
(`ai.service.shared_secret`). Unsigned calls that reach the service directly are verified
against Airflow and resolved through the **same IAM strategies** as the main server: they
need `ai:use`, and the AI role (viewer/editor/admin) follows `code:execute` / `ai:admin`.
There is no implicit admin for direct callers.
