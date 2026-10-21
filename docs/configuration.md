# Configuration

How AutoAir reads its settings: sources, precedence, configuration file formats, feature
flags, and the credential and governance rules of git deploys. Every setting (environment
variable, configuration-file key, type, default, description, secret flag) is listed in the
generated [configuration reference](configuration-reference.md). Task-oriented guides are on
the documentation site: [Configuration](https://ctolon.github.io/autoair/docs/admin/configuration) and the pages linked under
[Settings by area](#settings-by-area).

Settings are environment variables with the `AUTOAIR_` prefix (or a `.env` file) and/or
configuration files. Nested settings use `__`, e.g. `AUTOAIR_STORAGE__GIT__MODE`. Lists and maps
are JSON in environment variables (`'["dev","prod"]'`).

## Sources and precedence

Highest precedence first:

1. explicit arguments (`Settings(...)`, CLI options such as `autoair serve --dags-folder`);
2. environment variables (`AUTOAIR_*`);
3. the `.env` file of the working directory;
4. the secrets directory (pydantic-settings `secrets_dir`, when configured);
5. **configuration files** — later files override earlier ones;
6. defaults.

Values merge per key: `AUTOAIR_STORAGE__GIT__MODE=pr` overrides only `storage.git.mode` of a file
that sets the whole `storage` section.

### Configuration files

Name them (all are loaded, in this order):

* `[autoair] config_file = …` in `airflow.cfg` (or `AIRFLOW__AUTOAIR__CONFIG_FILE`) — read by the
  Airflow plugin and standalone alike;
* `AUTOAIR_CONFIG_FILE` — one reference, a comma separated list or a JSON list;
* `autoair --config FILE <command>` / `autoair serve --config FILE` (repeatable; appended to
  `AUTOAIR_CONFIG_FILE`, so subprocesses such as catalog builds see the same files).

| Reference | Format |
|---|---|
| `/etc/autoair/autoair.yaml`, `.yml` | YAML (PyYAML; `pip install 'autoair[yaml]'`; an error names the extra when it is missing) |
| `/etc/autoair/autoair.json` | JSON |
| `/etc/autoair/autoair.ini`, `.cfg`, `.conf` (or `airflow.cfg` itself) | INI, Airflow style |
| `/etc/autoair/settings.py`, `/etc/autoair/settings.py:attr` | Python file |
| `acme.autoair_settings`, `acme.autoair_settings:attr` | importable Python module |

YAML/JSON/Python use nested mappings with the keys of the reference (`storage.git.mode` is
`{"storage": {"git": {"mode": ...}}}`); a single top-level `autoair:` key may wrap everything.
A Python attribute is a dict, a `Settings` object (only the values it was given are used) or a
callable returning one of those; without `:attr` the attributes `AUTOAIR_CONFIG`, `config` and
`settings` are tried in that order.

**INI mapping**: `[autoair]` holds top-level keys, `[autoair.<a>.<b>]` sections hold nested ones
(`[autoair.storage.git]` → `storage.git.*`), and a key may be dotted (`git.mode = push` inside
`[autoair.storage]`). Values starting with `[`, `{` or `"` are JSON (lists, maps); everything
else is a string converted by type (`true`, `15`, `/path`). Other sections are ignored, so the
`[autoair*]` sections can live in `airflow.cfg`.

```yaml
# /etc/autoair/autoair.yaml
subfolder: autoair
expose_config: non-sensitive-only
features: {marketplace: false, python_code: true}
policy:
  dag_id_pattern: "[a-z][a-z0-9_]*"
storage:
  mode: git
  git: {provider: github, repo: acme/dags, mode: pr, pr_labels: [autoair], pr_draft: true}
discovery:
  skip_dirs_extra: [build, dist]
```

```ini
# airflow.cfg (or any .ini)
[autoair]
subfolder = autoair
required_tags = ["team"]

[autoair.storage.git]
provider = gitlab
repo = data/dags
mode = push

[autoair.features]
marketplace = false
```

```python
# acme/autoair_settings.py  ->  AUTOAIR_CONFIG_FILE=acme.autoair_settings
import os

def AUTOAIR_CONFIG():
    return {"team": os.environ.get("TEAM", "data"), "storage": {"history_limit": 50}}
```

Invalid values name the source: `storage.git.mode: Input should be 'pr' or 'push' (from
/etc/autoair/autoair.yaml)`; unknown top-level keys of a file are logged and ignored. Keep
credentials in environment variables / secret stores rather than in files readable by DAG code.

`autoair config show [--changed] [--json]` prints every effective value with its source.

### Exposing the configuration

`GET /api/config` (admins only) returns every effective value with its source (`default`, `env`,
`dotenv`, `init`, `file:<path>`, `ui` for a deploy target chosen in the UI), type, default and
description. Like Airflow's `[api] expose_config` it is off by default:
`AUTOAIR_EXPOSE_CONFIG=false` (403), `true` (secrets masked as `***`) or `non-sensitive-only`
(secrets left out). Secrets are `SecretStr` settings, fields marked
`json_schema_extra={"secret": True}` (e.g. `storage.storage_options`) and credential-like names
(`*token`, `*password`, `*passwd`, `*secret`, `*api_key`, `*private_key`, `*credential`, `*credentials`, `*headers`); nested models inside
maps (`team_storage`) are masked field by field.

## Feature flags

`AUTOAIR_FEATURES__<NAME>=false` switches a feature off: its API routes answer 404, the CLI
commands refuse (exit code 2) and `/api/meta` reports `features` so the UI hides it.

| Flag | Default | Covers |
|---|---|---|
| `ai`, `mcp` | `true` | AI assistance; the MCP endpoint `/mcp` |
| `kb` | `true` | the documentation knowledge base: `/api/kb/*`, the MCP tools `kb_search`, `kb_sources`, `kb_status`, documentation hits of `search_docs`, `autoair kb`, scheduled refreshes |
| `collab` | `true` | live co-editing (`/collab`, collaboration tickets; needs `autoair[collab]`) |
| `marketplace` | `true` | `/api/marketplace/*`, `autoair marketplace keygen/build` |
| `templates` | `true` | `/api/templates/*` |
| `imports` | `true` | `/api/interchange/*/import`, `autoair import-python/import-yaml` |
| `test_runs` | `true` | `/api/runner/*` (task/DAG test runs) |
| `runs` | `true` | trigger, backfill, clear, pause and asset events (`POST /api/airflow/dags/{dag_id}/trigger`, `POST /api/airflow/dags/{dag_id}/backfill`, `POST /api/airflow/dags/{dag_id}/runs/{run_id}/clear`, `PUT /api/airflow/dags/{dag_id}/paused`, `PUT /api/airflow/dags/{dag_id}/scheduling-state`, `POST /api/airflow/assets/{asset_id}/events`, `POST /api/airflow/assets/{asset_id}/materialize`) |
| `deploy` | `true` | saving/deleting specs (`PUT/DELETE /api/specs/<id>`), environment promotion |
| `custom_icons` | `true` | uploading/deleting custom icons |
| `python_code` | `true` | Python code in DAGs; also off while the policy switch `allow_python_code` is `false` |
| `airflow` | `true` | features that call an Airflow server: `/api/airflow/*` (connection/pool/variable pickers, DAG status, import from Airflow); off in `autoair local` |
| `iam` | `true` | AutoAir's own authorization (roles, mapping, approvals, direct-push roles); off = permissions come from Airflow only, see [iam.md](iam.md#switching-iam-off-autoair_features__iamfalse) |
| `visual_editor`, `code_editor` | `true` | the canvas editor and direct code editing (code mode, raw code DAGs); at least one stays on |
| `code_edit_inline` | `false` | an editable Code tab in the bottom panel |
| `source_preserve` | `true` | source-preserving deploys: hand-written DAG files are patched instead of regenerated (source policy preserve / strict-preserve, the `preserve` section) |
| `strict_source_preserve` | `false` | every preserve deploy runs as strict-preserve |
| `ide_language_server` | `false` | language-server completions in code editors (the `lsp` section) |
| `blueprints` | `true` | Astronomer Blueprint steps: the palette's Blueprints section, `/api/blueprints/*`, the MCP tool `list_blueprints` (the `blueprints` section; schemas need `autoair[blueprint]`) |

### Switching flags at runtime (admin panel)

Admins with `admin:config` can switch flags on and off without a restart in the admin panel's
**Features** tab (`GET /api/admin/features`, `PUT /api/admin/features/<name>` with
`{"value": true|false|null, "reason": "..."}`; `null` removes the override). Precedence, highest
first:

1. **locked**: flags matching `features_locked` (fnmatch patterns, `*` = every flag) or set through
   the environment (`AUTOAIR_FEATURES__<NAME>`, `.env`, secrets directory) keep their configured
   value; the panel shows them as locked and the API answers 409;
2. the **runtime override** set in the panel;
3. the **configured value** (configuration files, defaults).

The policy switch `allow_python_code` applies on top: an override cannot switch Python code on
while it is false. At least one of `visual_editor` and `code_editor` must stay on (422).

Overrides are stored in the state backend (`database.backend`: a `feature-overrides.json` file in
the state folder, or a row of the SQL database), so every replica sharing it applies them; a
replica re-reads them at most every `features_override_ttl` seconds (default 5; the replica that
made the change applies it at once). Every change is audited as `feature_override` (user, old and
new effective value, old and new override, reason). Routes are gated per request, including the
`/collab` WebSocket, `/mcp` and the AI routes mounted at start-up (they are only left unmounted
when the flag is locked off); `/api/meta` reports the new value at once and the UI re-reads it
when the window regains focus and every minute.

## Git tokens typed per deploy (`storage.git.request_token`)

Users can type their own git token in the deploy, promote and delete dialogs (a masked
**Git token** field with a show/hide toggle). The token travels only in the
`X-AutoAir-Git-Token` header of that one request (never the URL, query or body), is used for
that write and dropped: AutoAir never stores, caches, logs or audits it.

| Variable | Default | Description |
|---|---|---|
| `AUTOAIR_STORAGE__GIT__REQUEST_TOKEN` | `optional` | `off`: the header is refused (400) · `optional`: a token, when given, replaces the configured credentials for that one write ("Use my own token") · `required`: every git write needs one; the configured connection, if any, only serves reads (none configured: reads are anonymous) |
| `AUTOAIR_STORAGE__GIT__REQUEST_TOKEN_OVERRIDES_FORCED` | `false` | A token may replace `forced_conn_id` for that write. Default: the forced connection wins and the header is refused |
| `AUTOAIR_STORAGE__GIT__REQUEST_TOKEN_ALLOW_INSECURE` | `false` | Accept the header over plain HTTP from a non-loopback client and send it to `http://` git hosts (test setups only) |
| `AUTOAIR_STORAGE__GIT__MCP_REQUEST_TOKEN` | `false` | MCP over HTTP: `save_dag_spec` / `delete_dag_spec` take the token from the `X-AutoAir-Git-Token` header of the MCP request (see below). Off: such a header is refused |

**Which credentials a git write uses** (first match):

1. `request_token=off` on the global storage settings (a kill switch for team targets too), or
   the target has `forced_conn_id` and `request_token_overrides_forced=false`: the header is
   refused with 400 and the rules below 2. apply;
2. a token in the header: only that token (no GitHub App key, SSH key, app-password user,
   connection or configured token). A token the host refuses is a **401** (unknown/expired)
   or **403** (missing scopes) with `detail.code = "git_token_rejected"`; AutoAir never
   retries with other credentials;
3. no token and `required`: 400 `git_token_required`; otherwise the configured credentials — `forced_conn_id`,
   the user's own connection (`credentials_mode=per_user`), then the shared connection/token.

**Teams.** A DAG whose team has its own target (`team_storage.<team>`) uses that target's
`git.request_token*` settings; `off` on the global settings still turns tokens off everywhere.
Like every policy field, these settings cannot be changed from the deploy-target dialog.

**Providers.** GitHub, GitLab, Bitbucket and Azure DevOps use the token for their REST APIs
(Bitbucket as an access token, Azure DevOps as a PAT); `local` and `gitea` push with the git
CLI over HTTPS with the token as Basic credentials in `http.extraHeader`, passed through
`GIT_CONFIG_*` environment variables (never argv, the remote URL or `.git/config`; credential
helpers are disabled for that push), and Gitea opens pull requests with it. SSH remotes are
refused (a token cannot authenticate them), and so are `http://` hosts. The UI shows what to
create (from `GET /api/deploy-target/git-credentials` → `request_token.hint`):

| Provider | Token | Scopes |
|---|---|---|
| github | fine-grained personal access token (or classic with `repo`) | `contents:write`, `pull_requests:write` |
| gitlab | personal, project or group access token | `api` (`write_repository` alone only allows `git push`; AutoAir commits over the REST API) |
| bitbucket | repository, project or workspace access token | `repository:write`, `pullrequest:write` |
| azure | personal access token | Code (Read & write) |
| gitea | access token | `write:repository` (+ `read:user`, optional, for audit attribution) |
| local | the host's token for HTTPS pushes | write access |

**Transport.** The header is only accepted over HTTPS or from a loopback client. The scheme
and client address are what uvicorn reports after `server.proxy_headers` /
`server.forwarded_allow_ips` (behind a TLS-terminating proxy, list it there so
`X-Forwarded-Proto: https` is trusted); otherwise the request is refused with 403
`git_token_insecure` — revoke a token that crossed an untrusted network.

**Audit.** Writes record `credential: request-token` and, when the host tells it cheaply,
`credential_user` (the token owner's login from `GET /user` on GitHub/GitLab/Gitea/Bitbucket,
`connectionData` on Azure DevOps; best effort, never blocking the write). Other writes record
`credential: connection` and `credentials_conn_id`.

**MCP.** MCP tools never take a token as a tool argument: arguments are written by the model
and end up in its context, agent transcripts and tool-call logs. With
`storage.git.mcp_request_token=true`, MCP **over HTTP** reads it from the
`X-AutoAir-Git-Token` header of the MCP request, configured once in the MCP client's server
entry (like `Authorization`), with the same rules (modes, forced connection, HTTPS, masking,
audit with `via: mcp`). Over stdio there is no header. With `required` and no header, MCP
writes are refused (dry runs still answer). There is no MCP promote tool; promotions go
through the UI/REST API. With `ai.service_url` + `proxy_mcp`, the proxy does not forward the
header (MCP request tokens need the in-process MCP endpoint).

`GET /api/deploy-target` (`request_token`, `policy.request_token*`), `GET /api/me`
(`deploy.git_token`) and `GET /api/deploy-target/git-credentials?team=` tell the UI the mode.

## Deploy governance

Every commit and pull request AutoAir creates carries who made it and what was checked
(`autoair/governance.py`):

* the user's **message** (deploy dialog; first line = subject) or `commit_message_template`,
* the mandatory **context block** — always appended, users cannot remove it:

  ```text
  --- AutoAir deploy context ---
  Action: deploy
  DAG: etl_daily (environment prod)
  User: alice (Alice Smith <alice@acme.io>)
  Spec version: 1a2b3c4d5e6f -> 9f8e7d6c5b4a
  Validation: passed (0 errors, 2 warnings, 0 infos, import check ok)
  Approved by: bob
  Target: github:acme/dags@main (pr)
  AutoAir: 0.1.0
  Link: https://autoair.acme.io/ui/?dag=etl_daily
  ```

* **git trailers** as the last paragraph: `X-AutoAir-User`, `X-AutoAir-User-Name`,
  `X-AutoAir-Dag`, `X-AutoAir-Environment`, `X-AutoAir-Spec-Etag`, `X-AutoAir-Base-Etag`,
  `X-AutoAir-Validation`, `X-AutoAir-Version`, `Approved-by` (one per approver) and
  `Co-authored-by` when the service identity authors the commit (`git log
  --format='%(trailers:key=X-AutoAir-User)'` lists who deployed what).

The pull request description is `pr_body_template` — `{message}`, `{description}` (the PR
description typed in the deploy dialog) — with the context block appended unless the template
places `{context}` itself. All commit/PR templates accept the context placeholders `{user}`,
`{user_name}`, `{user_email}`, `{autoair_version}`, `{etag_before}`, `{etag_after}`,
`{validation}`, `{approvals}`, `{link}`, `{target}`, `{description}`, `{context}`. User-supplied
values are reduced to one line (no forged trailers). The deploy dialog shows a live preview
(`POST /api/specs/{dag_id}/deploy-preview`). Local-folder and object-storage targets store the
same message with each version. The link needs `AUTOAIR_SERVER__PUBLIC_URL`. Deploys, pull
requests, direct pushes, deletes, promotions, proposals and approvals are audited with the
same context (see [IAM § audit](iam.md#8-audit)).

## Settings by area

| Area | Guide | Reference |
|---|---|---|
| Core, storage and deploy targets | [Configuration](https://ctolon.github.io/autoair/docs/admin/configuration#storage-and-deploy-target), [Deploy](https://ctolon.github.io/autoair/docs/user-guide/deploy), [Git bundles](https://ctolon.github.io/autoair/docs/deployment/git-bundles) | [top level](configuration-reference.md#top-level), [`storage`](configuration-reference.md#storage), [`storage.git`](configuration-reference.md#storagegit) |
| Import checks, validator service | [Validation](https://ctolon.github.io/autoair/docs/user-guide/validation#import-check-validate-in-airflow), [Validator service](https://ctolon.github.io/autoair/docs/deployment/validator-service), [import-checks.md](architecture/import-checks.md) | [top level](configuration-reference.md#top-level) (`import_check_*`, `dagbag_pool*`), [`validator`](configuration-reference.md#validator) |
| Editing existing Python DAGs (source policies, files changed outside AutoAir) | [Editing existing Python DAGs](https://ctolon.github.io/autoair/docs/user-guide/editing-python-dags), [architecture](architecture/source-preserving-edits.md) | [`preserve`](configuration-reference.md#preserve), [`features`](configuration-reference.md#features) |
| Policy and OPA | [Policy](https://ctolon.github.io/autoair/docs/admin/policy), [policy.md](policy.md) | [`policy`](configuration-reference.md#policy) |
| Identity, roles, approvals, audit | [IAM](https://ctolon.github.io/autoair/docs/admin/iam), [Audit](https://ctolon.github.io/autoair/docs/admin/audit), [iam.md](iam.md) | [`iam`](configuration-reference.md#iam), [`audit`](configuration-reference.md#audit) |
| Sign-in with the Airflow session | [SSO](https://ctolon.github.io/autoair/docs/admin/sso), [sso.md](sso.md) | [`sso`](configuration-reference.md#sso) |
| Catalog, operators, icons | [Custom operators](https://ctolon.github.io/autoair/docs/extending/custom-operators), [Operator metadata](https://ctolon.github.io/autoair/docs/extending/operator-metadata) | [`catalog`](configuration-reference.md#catalog), [`discovery`](configuration-reference.md#discovery), [`icons`](configuration-reference.md#icons) |
| Astronomer Blueprint templates | [Blueprint steps](https://ctolon.github.io/autoair/docs/user-guide/blueprints) | [`blueprints`](configuration-reference.md#blueprints) |
| Environments and teams | [Environments](https://ctolon.github.io/autoair/docs/user-guide/environments), [Multi-team](https://ctolon.github.io/autoair/docs/user-guide/multi-team) | [top level](configuration-reference.md#top-level) |
| State store | [Database](https://ctolon.github.io/autoair/docs/admin/database), [Scaling](https://ctolon.github.io/autoair/docs/admin/scaling), [state-and-scaling.md](architecture/state-and-scaling.md) | [`database`](configuration-reference.md#database) |
| Server, TLS, HTTP headers, outbound connections | [TLS and HTTP headers](https://ctolon.github.io/autoair/docs/admin/tls-and-headers) | [`server`](configuration-reference.md#server), [`outbound`](configuration-reference.md#outbound) |
| Logging, metrics, tracing, rate limits, UI | [Observability](https://ctolon.github.io/autoair/docs/admin/observability) | [`logging`](configuration-reference.md#logging), [`limits`](configuration-reference.md#limits), [`ui`](configuration-reference.md#ui) |
| Collaboration | [Collaboration](https://ctolon.github.io/autoair/docs/user-guide/collaboration) | [`collab`](configuration-reference.md#collab) |
| Marketplace | [Marketplace](https://ctolon.github.io/autoair/docs/extending/marketplace) | [top level](configuration-reference.md#top-level) |
| Editors and language servers | [Editors](https://ctolon.github.io/autoair/docs/user-guide/editors) | [`editor`](configuration-reference.md#editor), [`lsp`](configuration-reference.md#lsp) |
| AI and MCP | [AI providers](https://ctolon.github.io/autoair/docs/ai/providers), [MCP](https://ctolon.github.io/autoair/docs/ai/mcp), [ai-architecture.md](ai-architecture.md), [mcp.md](mcp.md) | [`ai`](configuration-reference.md#ai) |
| Documentation knowledge base | [Knowledge base](https://ctolon.github.io/autoair/docs/ai/knowledge-base) | [`kb`](configuration-reference.md#kb) |
