---
title: Command-line reference
sidebar_label: CLI
description: Every autoair command and option - servers, validation and code generation, import and export, custom node scaffolding, catalog, marketplace, IAM, configuration and database commands - with exit codes and examples.
---

The `autoair` command is installed with the Python package. Run `autoair --help` for the command list and `autoair COMMAND --help` (or `autoair GROUP COMMAND --help`) for the options of the installed version.

```text
autoair [--config FILE]... COMMAND [ARGS]...
autoair --version
```

## Global options

| Option | Description |
|---|---|
| `--config`, `-c FILE` | Configuration file: YAML, JSON, INI, `.py[:attr]` or `module:attr`. Repeatable. The files are added after `AUTOAIR_CONFIG_FILE` (later files override earlier ones), and environment variables still win. They are also passed to subprocesses AutoAir starts. See [Configuration](/docs/admin/configuration#configuration-files) |
| `--version` | Print `autoair <version>` (for example `autoair 0.1.0`) and exit |
| `--install-completion` | Install shell completion for the current shell |
| `--show-completion` | Print the completion script, to copy or customise it |
| `--help` | Show help and exit |

`serve` and `local` also accept `--config` after the command name.

:::danger Python configuration runs code
A Python configuration file or module is executed with the privileges of the `autoair` process. Load only administrator-controlled files.
:::

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | The command ran and found a problem: validation failed, a signature is invalid, a YAML export is lossy, a database operation failed |
| `2` | The command could not run: invalid configuration (the message names the setting and its source), a disabled feature (`the <name> feature is disabled (AUTOAIR_FEATURES__<NAME>)`), invalid TLS options, a `file` state backend for `db` commands, an invalid or existing `new-node` target, invalid arguments |

## Commands at a glance

| Command | Purpose |
|---|---|
| [`serve`](#autoair-serve) | Run the standalone server (UI and API) next to an Airflow deployment |
| [`local`](#autoair-local) | Run AutoAir on a workstation without an Airflow server |
| [`generate`](#autoair-generate) | Generate the DAG Python file from a spec |
| [`validate`](#autoair-validate) | Validate a spec (catalog, graph, policy, DagBag import) |
| [`import-python`](#autoair-import-python) | Convert a hand-written Python DAG file into specs |
| [`import-yaml`](#autoair-import-yaml) / [`export-yaml`](#autoair-export-yaml) | Convert between specs and Airflow 3.4 YAML Dags |
| [`export-dag-factory`](#autoair-export-dag-factory) | Export a spec as dag-factory YAML |
| [`export-schema`](#autoair-export-schema) / [`export-openapi`](#autoair-export-openapi) | Write the spec JSON Schema / the API's OpenAPI document |
| [`new-node`](#autoair-new-node) | Scaffold a Python package that registers a custom node kind |
| [`catalog`](#autoair-catalog) | Build or dump the operator catalog |
| [`marketplace`](#autoair-marketplace) | Create keys for, build and verify signed template indexes |
| [`iam`](#autoair-iam) | Show roles; register permissions in Airflow's FAB auth manager |
| [`config`](#autoair-config) | Show the effective configuration and its sources |
| [`db`](#autoair-db) | Migrate and inspect the SQL state store |
| [`kb`](#autoair-kb) | Manage and search the documentation knowledge base; build and import bundles |
| [`ai-serve`](#autoair-ai-serve) | Run the AI service on its own |
| [`validator-serve`](#autoair-validator-serve) | Run the validator service for isolated import checks |
| [`mcp`](#autoair-mcp) / [`mcp-serve`](#autoair-mcp-serve) | Run the MCP server (stdio or HTTP) |
| [`docs-mcp`](#autoair-docs-mcp) | Serve AutoAir's own documentation over MCP for coding agents |

## Servers

### `autoair serve`

Run the builder as a standalone service: the UI at `/ui/` and the API at `/api` (OpenAPI at `/api/docs`). Users authenticate with their Airflow session or token; see [SSO and sessions](/docs/admin/sso).

```text
autoair serve [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--host TEXT` | `AUTOAIR_SERVER__HOST` (`127.0.0.1`) | Bind address |
| `--port INTEGER` | `AUTOAIR_SERVER__PORT` (`8765`) | Port |
| `--dags-folder PATH` | `AUTOAIR_DAGS_FOLDER` | Folder of the local deploy target |
| `--airflow-url TEXT` | `AUTOAIR_AIRFLOW_URL` | Airflow REST API base URL |
| `--no-auth` / `--no-no-auth` | `--no-no-auth` | Do not require a token. Every caller gets `iam.anonymous_roles`. Development only |
| `--workers INTEGER` | `AUTOAIR_SERVER__WORKERS` (`1`) | Worker processes; more than one needs shared state (see [Scaling](/docs/admin/scaling)) |
| `--ssl-certfile PATH` | unset | TLS certificate chain (PEM); enables HTTPS |
| `--ssl-keyfile PATH` | unset | TLS private key (PEM). Its password only through `AUTOAIR_SERVER__SSL_KEYFILE_PASSWORD` |
| `--ssl-ca-certs PATH` | unset | CA bundle for client certificates (mutual TLS) |
| `--ssl-cert-reqs TEXT` | `none` | Client certificates: `none`, `optional`, `required` |
| `--forwarded-allow-ips TEXT` | `127.0.0.1` | Comma-separated proxy addresses trusted for `X-Forwarded-*` (`*`: all) |
| `--config`, `-c TEXT` | | Configuration file; repeatable |

The other server options are settings of the [`server` section](/docs/reference/configuration#server) without a CLI option, among them `AUTOAIR_SERVER__SSL_MIN_VERSION` and `AUTOAIR_SERVER__SSL_CIPHERS` (TLS), `AUTOAIR_SERVER__SECURITY_HEADERS`, `AUTOAIR_SERVER__CONTENT_SECURITY_POLICY`, `AUTOAIR_SERVER__FRAME_ANCESTORS` and `AUTOAIR_SERVER__REFERRER_POLICY` (headers), `AUTOAIR_SERVER__HSTS_SECONDS`, `AUTOAIR_SERVER__HSTS_INCLUDE_SUBDOMAINS` and `AUTOAIR_SERVER__HSTS_PRELOAD` (HSTS), `AUTOAIR_SERVER__KEEP_ALIVE_TIMEOUT` and `AUTOAIR_SERVER__GRACEFUL_SHUTDOWN_TIMEOUT` (timeouts) and `AUTOAIR_SERVER__MAX_BODY_BYTES` (body limit); see [TLS and HTTP headers](/docs/admin/tls-and-headers). On start-up the command prints `AutoAir: writing DAGs to <folder>; Airflow at <url>`. Inconsistent TLS options exit with code `2`.

```bash
autoair serve --dags-folder /opt/airflow/dags --airflow-url http://airflow:8080
autoair serve --host 0.0.0.0 --workers 4 \
  --ssl-certfile /etc/autoair/tls.pem --ssl-keyfile /etc/autoair/tls.key \
  --forwarded-allow-ips 10.0.0.10 -c /etc/autoair/autoair.yaml
```

### `autoair local`

Try AutoAir on a laptop without an Airflow server (`uvx --from 'autoair[standalone,airflow]' autoair local`): the operator catalog of the installed packages, static validation (plus the DagBag import check when Airflow is installed), deploys to a local folder (or git when `AUTOAIR_STORAGE__MODE=git`). Runs, test runs and Airflow pickers are off. See [Local mode](/docs/installation/local).

```text
autoair local [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `--dags-folder PATH` | `./dags` | Folder DAGs are written to |
| `--host TEXT` | `127.0.0.1` | Bind address. Binding a non-loopback address without `--token` is refused (exit `2`) |
| `--port INTEGER` | `8765` | Port |
| `--token TEXT` | `AUTOAIR_LOCAL__TOKEN` | Require this access token; paste it on the sign-in page |
| `--open` / `--no-open` | `AUTOAIR_LOCAL__OPEN_BROWSER` (`false`) | Open the UI in the browser once the server answers |
| `--config`, `-c TEXT` | | Configuration file; repeatable |

```bash
uvx --from 'autoair[standalone,airflow]' autoair local --open
uvx --from 'autoair[standalone,airflow]' autoair local --dags-folder ~/dags --token "$(openssl rand -hex 16)"
uvx --from 'autoair[standalone,airflow]' --with apache-airflow-providers-amazon autoair local   # add a provider to the catalog
```

The HTTP server comes with the `standalone` extra. Without it, `autoair local` (like `serve`, `mcp-serve --http` and `ai-serve`) exits with code `1` and names the extra to install, for example:

```text
`autoair local` needs an HTTP server, which comes with the 'standalone' extra (uvicorn). Install it with: pip install 'autoair[standalone,airflow]'
or run it with uv: uvx --from 'autoair[standalone,airflow]' autoair local --open
```

`airflow` is suggested as long as Airflow is not installed: without it the palette has no operators.

### `autoair ai-serve`

Run the AI service on its own: the AI API at `/api/ai` and MCP at `/mcp`. Point the main server at it with `AUTOAIR_AI__SERVICE_URL=http://HOST:PORT`. See [AI service](/docs/ai/ai-service).

| Option | Default | Description |
|---|---|---|
| `--host TEXT` | `127.0.0.1` | Bind address |
| `--port INTEGER` | `8790` | Port |
| `--no-auth` / `--no-no-auth` | `--no-no-auth` | Do not require a bearer token |

```bash
autoair ai-serve --host 0.0.0.0 --port 8790
```

### `autoair validator-serve`

Run the validator service: DagBag import checks of untrusted DAG code in sandboxed worker processes (`POST /v1/check`, `/health`, `/ready`, `/metrics`). Point the main server at it with `AUTOAIR_VALIDATOR__URL=http://HOST:PORT`; both sides share an HMAC key, the password of the Airflow connection `validator.secret_conn_id`, given to this service as `AIRFLOW_CONN_<CONN_ID>`. See [Validator service](/docs/deployment/validator-service).

| Option | Default | Description |
|---|---|---|
| `--host TEXT` | `127.0.0.1` | Bind address |
| `--port INTEGER` | `8791` | Port |
| `--no-auth` / `--no-no-auth` | `--no-no-auth` | Accept unsigned checks (tests only: anyone who reaches the port can run code) |
| `--config`, `-c TEXT` | none | Configuration file; repeatable |

Exit codes: `1` when Apache Airflow is not installed, `2` when no key is configured and `--no-auth` is not given.

```bash
autoair validator-serve --host 0.0.0.0 --port 8791
```

### `autoair mcp`

Run the AutoAir MCP server: stdio by default (for a local agent that starts the process), or unauthenticated streamable HTTP for local use. For a shared endpoint prefer `autoair mcp-serve --http`. See [MCP](/docs/ai/mcp).

| Option | Default | Description |
|---|---|---|
| `--http` / `--no-http` | `--no-http` | Serve streamable HTTP instead of stdio (no authentication) |
| `--host TEXT` | `127.0.0.1` | HTTP bind address |
| `--port INTEGER` | `8766` | HTTP port |

### `autoair mcp-serve`

Standalone MCP server. In HTTP mode it uses the same authentication as `autoair serve` (the caller's Airflow bearer token, verified against Airflow) at `http://HOST:PORT/mcp/`.

| Option | Default | Description |
|---|---|---|
| `--http` / `--no-http` | `--no-http` | Serve streamable HTTP instead of stdio |
| `--host TEXT` | `127.0.0.1` | Bind address |
| `--port INTEGER` | `8766` | Port |
| `--no-auth` / `--no-no-auth` | `--no-no-auth` | HTTP: do not require a bearer token |

```bash
autoair mcp-serve --http --host 0.0.0.0 --port 8766
```

### `autoair docs-mcp`

An MCP server with only the documentation tools (`search_autoair_docs`, `read_autoair_doc`,
`list_autoair_docs`) over the documentation packaged with the installed version: no Airflow,
settings, state or network. Needs the `mcp` extra; over HTTP it has no authentication. See
[AutoAir docs MCP](/docs/ai/docs-mcp).

| Option | Default | Description |
|---|---|---|
| `--http` | off | Serve streamable HTTP instead of stdio |
| `--host HOST` | `127.0.0.1` | HTTP bind address |
| `--port PORT` | `8767` | HTTP port |
| `--path PATH` | `/mcp` | HTTP endpoint path |

```bash
uvx --from 'autoair[mcp]' autoair docs-mcp --http --port 8767
```

## Specs and code

These commands work on spec files (`<dag_id>.autoair.json`, the format described in [Spec format](/docs/extending/spec-format)).

### `autoair generate`

Generate the DAG Python source from a spec file. Prints to standard output unless `-o` is given.

```text
autoair generate [-o OUTPUT] SPEC
```

```bash
autoair generate etl_daily.autoair.json -o dags/etl_daily.py
```

### `autoair validate`

Validate a spec: catalog, graph, policy and, by default, an Airflow DagBag import. Each problem is printed as `SEVERITY code [node.field]: message`, followed by `OK` or `FAILED`. Exit code `0` when valid, `1` otherwise.

| Option | Default | Description |
|---|---|---|
| `--import-check` / `--no-import-check` | `--import-check` | Import the generated DAG with a DagBag (executes the DAG code in a subprocess; needs Airflow installed) |
| `--env TEXT` | `default_environment` | Validate for this environment: applies the DAG's overlay and the environment-specific [policy rules](/docs/admin/policy) |

```text
$ autoair validate --env prod etl_daily.autoair.json
ERROR   policy_rule [extract.operator]: BashOperator is not allowed in production [policy rule no-bash-in-prod]
FAILED
```

Use it in CI to check specs committed to git with the same policy as the server (run it with the same configuration).

### `autoair import-python`

Convert a hand-written Python DAG file into AutoAir specs. One `<dag_id>.autoair.json` is written per DAG found; warnings and failures go to standard error.

```text
autoair import-python [-o OUTPUT_DIR] PATH
```

| Option | Default | Description |
|---|---|---|
| `-o PATH` | `.` | Folder the specs are written to |

Importing **executes** the file (with `python_import_timeout`), so it requires `features.imports` and `features.python_code` (exit `2` otherwise). See [Import and export](/docs/user-guide/import-export).

### `autoair import-yaml`

Convert an Airflow 3.4 YAML Dag into an AutoAir spec, printed as JSON on standard output; issues go to standard error. Requires `features.imports`.

```bash
autoair import-yaml dags/etl.yaml > etl.autoair.json
```

### `autoair export-yaml`

Export a spec as an Airflow 3.4 native YAML Dag on standard output. Issues go to standard error; the exit code is `1` when the export is lossy (the spec uses something YAML Dags cannot express).

```bash
autoair export-yaml etl_daily.autoair.json > dags/etl_daily.yaml
```

### `autoair export-dag-factory`

Export a spec as dag-factory YAML on standard output; warnings go to standard error.

### `autoair export-schema`

Write the JSON Schema of the DAG spec.

| Option | Default |
|---|---|
| `--output PATH` | `dag-spec.schema.json` |

### `autoair export-openapi`

Write the API's OpenAPI document (the input of the TypeScript client generator).

| Option | Default |
|---|---|
| `--output PATH` | `frontend/openapi.json` |

The REST API is also documented on the [API reference](/api) page.

## `autoair new-node`

Scaffold a Python package that registers a custom node kind through the `autoair.node_kinds` entry point. The package contains the handler, `pyproject.toml`, a README and tests. See [Custom node SDK](/docs/extending/node-sdk).

```text
autoair new-node [OPTIONS] NAME
```

`NAME` is the node kind name, for example `http_get`: lowercase letters, digits and `_`, starting with a letter, at most 61 characters.

| Option | Default | Description |
|---|---|---|
| `--title TEXT` | `My node` | Palette title |
| `--description TEXT` | `A custom AutoAir node kind` | Palette description |
| `--category TEXT` | `custom` | Palette category |
| `--icon TEXT` | `builtin:python` | `builtin:<logo>` or `custom:<name>` |
| `--output`, `-o PATH` | `.` | Parent folder; the project folder is created inside it |

The project folder is `autoair-node-<name with - for _>`, the import package `autoair_node_<name>`, and the handler class `<Name>Handler` (for `http_get`: `autoair-node-http-get`, `autoair_node_http_get`, `HttpGetHandler`). The template is shipped in the AutoAir package (`src/autoair/scaffold/custom-node`) and rendered without cookiecutter; the same folder also works with `cookiecutter`.

```text
$ autoair new-node http_get --title "HTTP GET" --category http
created autoair-node-http-get
next: pip install -e 'autoair-node-http-get[test]' && pytest autoair-node-http-get  (then restart AutoAir)
```

```text
autoair-node-http-get/
  pyproject.toml
  README.md
  src/autoair_node_http_get/__init__.py
  src/autoair_node_http_get/handler.py
  tests/test_handler.py
```

An invalid name (`error: invalid node name ...`) or a project folder that already exists (`error: <folder> already exists`) exits with code `2`; nothing is overwritten.

## `autoair catalog`

The operator catalog lists every operator, sensor and notifier found in the installed packages and configured folders.

### `autoair catalog build`

Precompute the catalog, for example in a Docker build, and point `AUTOAIR_CATALOG_FILE` at the result for fast start-up. Prints `wrote <file>: N operators, M providers, K errors`.

| Option | Default | Description |
|---|---|---|
| `--output`, `-o PATH` | `autoair-catalog.json` | Output file |
| `--strategy [static\|import]` | `static` | `static` parses provider code without importing it; `import` imports and inspects |

```dockerfile
RUN autoair catalog build -o /opt/autoair/catalog.json
ENV AUTOAIR_CATALOG_FILE=/opt/autoair/catalog.json
```

### `autoair catalog dump`

Print the discovered catalog as JSON (summaries by default).

| Option | Default | Description |
|---|---|---|
| `--details` / `--no-details` | `--no-details` | Print the full entry of every operator (parameters, docs) |
| `--diagnostics` / `--no-diagnostics` | `--no-diagnostics` | Print the build diagnostics instead: scanned sources with counts, operators per source, what was filtered out and why, failed modules, applied package metadata, build time |

## `autoair marketplace`

Publish signed template indexes (Ed25519). `keygen` and `build` require `features.marketplace`. See [Publishing a marketplace](/docs/extending/marketplace).

### `autoair marketplace keygen`

Create a signing key and print the public key to put in `AUTOAIR_MARKETPLACE_SOURCES`. The private key file is written with mode `0600`.

| Option | Default |
|---|---|
| `--out PATH` | `marketplace.pem` |

### `autoair marketplace build`

Write `DIRECTORY/index.json` from `DIRECTORY/templates/*.json` (and `extensions.json`), signed.

```text
autoair marketplace build DIRECTORY --name TEXT [--key PATH] [--description TEXT]
```

| Option | Description |
|---|---|
| `--name TEXT` | Index name (required) |
| `--key PATH` | Ed25519 private key (PEM); without it the index is unsigned |
| `--description TEXT` | Index description |

### `autoair marketplace verify`

Check an index signature against a base64 Ed25519 public key. Prints `signature OK`, or `signature INVALID` with exit code `1`.

```bash
autoair marketplace keygen --out market.pem
autoair marketplace build ./market --name acme --key market.pem
autoair marketplace verify ./market/index.json --public-key "<base64 key>"
```

## `autoair iam`

AutoAir roles and permissions. See [IAM](/docs/admin/iam).

### `autoair iam roles`

Print every role (built-in and `iam.roles`) with its description and permissions, and the roles holding `deploy:direct_push`.

| Option | Description |
|---|---|
| `--json` | Print JSON |

### `autoair iam sync-fab`

Create AutoAir's permissions in Airflow's FAB auth manager, one per AutoAir permission and named as in the [IAM permission table](/docs/admin/iam#permissions) (for example `can_create on AutoAir Deploy`). Run it where Airflow runs: it uses Airflow's FAB application and metadata database. Idempotent; it only adds permissions.

| Option | Default | Description |
|---|---|---|
| `--create-roles` / `--no-create-roles` | `--no-create-roles` | Also create `AutoAir <Role>` roles holding the permissions. Refused when `iam.role_prefix` is empty |

```bash
airflow fab-db migrate
autoair iam sync-fab --create-roles
```

## `autoair config`

### `autoair config show`

Print every effective setting with its source (`default`, `env`, `dotenv`, `init`, `file:<path>`). Secrets are masked as `***`. Configuration files in effect are listed first as `# config file: <path>`.

| Option | Default | Description |
|---|---|---|
| `--json` | | Print JSON: `{"files": [...], "options": [{"key", "env", "value", "source"}]}` |
| `--changed` / `--no-changed` | `--no-changed` | Only values that differ from the defaults |

```bash
autoair config show --changed
autoair -c /etc/autoair/autoair.yaml config show --json | jq '.options[] | select(.key | startswith("storage."))'
```

See [Configuration](/docs/admin/configuration#autoair-config-show).

## `autoair db`

AutoAir's SQL state store (`database.backend` `sqlite` or `postgresql`). With the `file` backend both commands exit with `2` (`database.backend is 'file': nothing to migrate`). See [Database](/docs/admin/database#migrations-autoair-db).

### `autoair db upgrade`

Create or migrate AutoAir's tables. Safe to run from several replicas at once (an advisory lock serialises them). Prints `applied: [...]; head: N`.

| Option | Default | Description |
|---|---|---|
| `--revision INTEGER` | latest | Target schema version; a version above the latest exits with `2` |

### `autoair db current`

Print the applied schema version, the latest one and pending migrations (`<masked URL>: version N (head M)`, then `pending: [...] - run autoair db upgrade` when there are any).

| Option | Description |
|---|---|
| `--json` | Print `{"url", "current", "head", "pending"}` |

A database that cannot be reached exits with `1` and prints the masked URL with the first line of the error.

## `autoair kb`

The [documentation knowledge base](/docs/ai/knowledge-base). Every subcommand except `keygen` requires `features.kb` (exit `2` otherwise); none needs the `ai` extra.

| Command | Options | Description |
|---|---|---|
| `autoair kb sources` | `--json` | The configured sources (with admin-panel overrides) and the known components |
| `autoair kb status` | `--json` | Index size, search mode and the last refresh |
| `autoair kb refresh` | `--rebuild` / `--no-rebuild` (default `--no-rebuild`), `--source`, `-s ID` (repeatable), `--json` | Refresh the index (incremental; one replica at a time). `--rebuild` drops and re-indexes the sources |
| `autoair kb search QUERY` | `--component`, `--package`, `--version`, `--limit` (1-50, default `6`), `--json` | Search the index (keyword, plus embeddings when configured) |
| `autoair kb build` | `--out`, `-o PATH` (required), `--sign-key PATH`, `--store-path PATH` (default `<kb cache>/build.sqlite3`), `--source`, `-s ID`, `--online` / `--no-online`, `--description TEXT` | Fetch the configured sources and write a portable bundle (`.zip`, checksummed, optionally signed) |
| `autoair kb import BUNDLE` | `--source-id TEXT` (default `bundle`), `--json` | Import a bundle into the configured store; checksums are always verified, the signature when `kb.bundle.public_keys` is set |
| `autoair kb keygen` | `--out PATH` (default `kb-bundle.pem`) | Create a bundle signing key (PEM, mode `0600`) and print the public key for `kb.bundle.public_keys` |
| `autoair kb mcp` | | Serve only the knowledge-base tools over MCP (stdio) |

```bash
autoair kb keygen --out kb-bundle.pem
autoair kb build --online --sign-key kb-bundle.pem --out airflow-docs.zip
autoair kb import airflow-docs.zip
```

See [Knowledge base without internet access](/docs/ai/knowledge-base-air-gapped).

## Environment

Every command reads the same configuration as the server: `AUTOAIR_*` environment variables, `.env` in the working directory, configuration files from `airflow.cfg` (`[autoair] config_file`), `AUTOAIR_CONFIG_FILE` and `--config`. Run administrative commands (`config show`, `db`, `iam sync-fab`, `validate`) with the same environment as the server they concern. The full list of settings is in the [configuration reference](/docs/reference/configuration).
