# AutoAir

A visual and code-based DAG builder for Apache Airflow 3 (`>=3.0,<4`). Users design DAGs on a
canvas; AutoAir generates readable Python (or a JSON spec plus runtime loader, Airflow 3.4
YAML, or dag-factory YAML), validates it with Airflow's `DagBag`, and deploys it to a local
folder, object storage or git (push or pull request). Human contributors: CONTRIBUTING.md.

This file holds what applies to every task. Area rules live in `.claude/rules/` and load when
you open matching files; multi-step procedures are skills in `.claude/skills/`.

## Modes

One Python package, `autoair`; `autoair --help` lists the CLI (`autoair.cli:app`, Typer).

| Mode | Entry point | Notes |
|---|---|---|
| Airflow plugin | entry point `airflow.plugins` -> `autoair.plugin:AutoAirPlugin` | UI under Browse -> DAG Builder (3.1+, `react_apps`) or `/autoair/ui/` (3.0); API at `/autoair/api` |
| Standalone server | `autoair serve` (`autoair.server`) | API at `/api`, UI at `/ui/`; authenticates against an Airflow URL |
| Local mode | `autoair local` | laptop use without an Airflow server (`features.airflow` off) |
| AI service | `autoair ai-serve` (`autoair.ai.service`) | optional separate process for AI routes; trusts an HMAC identity or Airflow tokens |
| MCP server | `autoair mcp` (stdio), `autoair mcp-serve --http`, `autoair-mcp`; also mounted at `/mcp` | FastMCP 4 tools for authoring, validating and deploying DAGs |

## Stack

- Python >= 3.10 (CI 3.10-3.14; dev venv 3.12 from `.python-version`), uv, hatchling;
  FastAPI, Pydantic v2, pydantic-settings, Typer, Jinja2, fsspec; HTTP client `httpx2`.
- State: files, or SQLAlchemy 2 Core with SQLite or PostgreSQL (psycopg 3).
- Optional extras: `autoair[mcp]` (FastMCP >= 4 on `mcp` >= 2.3), `autoair[ai]`
  (`pydantic-ai-slim`) plus per-provider extras (`ai-anthropic`, `ai-openai`, ..., `ai-all`).
- Frontend: React 19, Chakra UI v3, @xyflow/react 12, zustand 5 + zundo, CodeMirror 6, RJSF 6,
  i18next, TanStack Query, Yjs, Vite 8, TypeScript 6, vitest, Playwright; Node >= 22.18.
- Docs site: Docusaurus 3.10.

## Repository map

| Path | Contents |
|---|---|
| `src/autoair/` | the Python package (module map: `.claude/rules/backend-python.md`) |
| `frontend/` | React editor; `npm run build` writes `src/autoair/static/{plugin,standalone}/` (gitignored, shipped in the wheel) |
| `tests/` | `unit/`, `integration/` (real Airflow), `golden/` (codegen snapshots), `live/` (real services) |
| `docs/` | in-repo reference (configuration, architecture, IAM/SSO, policy, MCP and AI internals; read on GitHub) |
| `website/` | user documentation (Docusaurus) published to GitHub Pages |
| `deploy/` | Dockerfiles, Compose stacks (`deploy/compose/README.md`), Helm values |
| `scripts/` | dev servers, doc generators, audits, release check |
| `.claude/` | rules, skills, subagents, hooks and settings for Claude Code |

## Setup and commands

```bash
uv sync                                    # Python 3.12 venv: Airflow 3.3 + dev group
uv sync --extra postgres --extra collab    # extras as needed (see [project.optional-dependencies])
(cd frontend && npm ci)
(cd website && npm ci)
```

| Task | Command |
|---|---|
| Standalone server | `uv run autoair serve --dags-folder <dir> --airflow-url <url> --port <free port>` |
| Local mode | `uv run autoair local --open` |
| MCP server (stdio) | `uv run autoair mcp` |
| Vite dev server | `cd frontend && AUTOAIR_API_PORT=<api port> npx vite --mode standalone --port <free port> --strictPort` |
| Airflow + plugin | `./scripts/dev-airflow.sh` (env `AIRFLOW_HOME`, `AIRFLOW_PORT`, `AIRFLOW_VENV`, `AUTOAIR_DEV_GIT_REPO`) |

Make targets: `make check` (all static checks), `make test` (pytest + vitest), `make matrix`
(nox), `make e2e`, `make crawl`, `make codegen`, `make build`, `make docs`, `make audit`.
`make dev-airflow`, `make serve`, `make ui-dev`, `make demo` and `make docs-serve` bind the
shared ports (see "Shared demo"): do not run them while the demo is up.

## Verify

Run fast to slow; stop at the first failure. Full sequences: skills `verify-backend`,
`verify-frontend`, `verify-ui-screenshots`, `run-e2e`; area rules add their own checks. Review
with skill `review-change` (it fans out to the reviewer subagents).

| Change | Commands |
|---|---|
| Python | `uv run ruff check src tests scripts deploy` · `uv run ruff format --check src tests scripts deploy` · `uv run mypy src` · `uv run pytest -q <focused tests>` · `uv run pytest -q --ignore=tests/live` |
| Frontend | `cd frontend`: `npm run typecheck` · `npm run lint` · `npm run format:check` · `npx vitest run <dir>` · `npm test` |
| Visible UI | screenshots light + dark, plugin + standalone (skill `verify-ui-screenshots`) |
| Docs | `cd website`: `node scripts/check-mdx.mjs <files>` · `npm run typecheck` · `npm run build` |

## Invariants

Each rule exists because breaking it has cost the project before or would break a
deployment. When a rule blocks a task, stop and ask the owner.

1. **No secrets stored in AutoAir.** Git tokens, LLM keys and passwords come from Airflow
   connections (`conn_id`, `forced_conn_id`, `user_conn_id_template`, `llm_conn_id`). A
   per-request token (`X-AutoAir-Git-Token`) lives in memory for one request only, runs under
   `secret_mask.masking`, and never reaches logs, audit records, error messages, persisted
   documents or model context. Secret settings that exist are `SecretStr`; adding one is the
   owner's decision. The one approved exception: opt-in per-user LLM keys, stored
   Fernet-encrypted (`ai/user_llm.py`). *Why:* AutoAir runs inside Airflow next to user code;
   Airflow's connection store is the one audited secret store.
2. **Airflow `>=3.0,<4`.** Feature-detect APIs newer than 3.0; generated code follows the
   target version. *Why:* users run every 3.x minor; CI covers 3.0-3.3 and 3.4 pre-releases.
   Skill: `airflow-compat-check`.
3. **Settings are documented and literals triaged.** Every setting has a `description` and a
   default; every literal under `src/autoair` is triaged in `scripts/hardcoded_decisions.json`.
   *Why:* the reference docs are generated from descriptions, and operators must be able to
   change any value that depends on their environment. Skills: `add-setting`,
   `hardcoded-audit-triage`.
4. **MCP tools are classified** in `src/autoair/mcp/iam.py`; unknown tools need `admin:config`,
   i.e. are denied to normal users, and a test enforces the classification. Skill:
   `add-mcp-tool`.
5. **API routes are gated** with `require(P.<permission>)` and, for user-facing features,
   `feature("<name>")`, evaluated per request. *Why:* admin overrides must apply without a
   restart. Skill: `add-api-route`.
6. **Outbound HTTP goes through `autoair/net/tls.py`**; user-supplied URLs also go through the
   SSRF checks in `autoair/ai/net.py`. *Why:* corporate TLS/proxy settings and SSRF protection
   apply only on that path.
7. **Multi-replica safe.** No process-local state that other replicas must see; shared state
   goes through `persistence`, collaboration through `collab_sync`; per-process caches are
   derived data with a TTL. *Why:* deployments run several replicas behind a load balancer.
   Skill: `add-persistence-document`.
8. **UI:** every user-visible string in English and Turkish; Chakra semantic colour tokens so
   both themes work; invalid live input never breaks a panel; document changes are undoable.
   *Why:* the plugin follows Airflow's theme, and panels render values while they are typed.
9. **Generated API types.** After backend API changes run `cd frontend && npm run codegen` and
   keep `frontend/openapi.json` and `frontend/src/api/gen/` in the same change. *Why:* CI
   regenerates and fails on a diff.
10. **Verify with tests, the crawler and Playwright E2E, not with fuzzing.**
11. **No commits, pushes, tags, releases or publishing unless the user asks** in the current
    conversation.
12. **Shared working tree.** Several agents edit this checkout at once. Never revert, stash,
    reset, clean or reformat files you did not change. A failing check in someone else's file
    is reported, not fixed.

## Shared demo

Other people and agents use these servers right now. Never restart, stop or kill them, and
never write test data to them:

| Port | Service |
|---|---|
| 8080 | Airflow API server with the plugin (`scripts/demo.sh`, `AIRFLOW_HOME=.airflow-dev`) |
| 8793, 8794 | Airflow log servers of the same instance |
| 8765 | standalone AutoAir (git mode) |
| 5173 | Vite dev server |
| 3000 | Docusaurus dev server |

- Use your own ports. `.claude/skills/run-e2e/scripts/private-stack.sh` starts a private
  `autoair serve` plus `vite preview` on a free pair in 18000-18899 with its own state folder.
- Kill only PIDs you started (`kill <pid>` or your own process group). Never `pkill -f`,
  `killall`, or kill by port.
- Tokens for scripts: `curl -s http://localhost:8080/auth/token` (simple auth manager, all
  admins). Reading from the shared Airflow is fine; deploying, triggering, deleting or changing
  connections there is not. Remove anything you created by mistake.
- `npm run build` rewrites the bundles the shared Airflow and AutoAir serve. Build to a private
  `--outDir` unless you intend to update the demo.
- `tests/conftest.py` defaults `AIRFLOW_HOME` to `.airflow-dev`, the demo's home: set
  `AIRFLOW_HOME=$(mktemp -d)` for runs that write Airflow state.
- Do not touch Docker resources you did not create (`deploy/compose` demo stack).

## Code style

Match the surrounding code; consistency beats preference. Comments explain *why* (a
constraint, a trade-off, a bug avoided), never *what* the next line does; no changelog-style
comments ("added X", "fixed Y") in code. User-visible changes get a CHANGELOG entry under
`[Unreleased]` and a docs update where the topic lives (`.claude/rules/docs-website.md`).

## Generated files

Never edited by hand (a PreToolUse hook blocks it and names the generator):

| File | Regenerate with |
|---|---|
| `frontend/src/api/gen/**`, `frontend/openapi.json` | `cd frontend && npm run codegen` |
| `docs/configuration-reference.md` | `uv run python scripts/gen_config_docs.py` |
| `website/docs/reference/configuration.md` (gitignored) | `cd website && npm run sync` |
| `website/static/openapi.json`, `website/static/schemas/dag-spec.schema.json` | `cd website && npm run export-api` |
| `tests/golden/expected/*.py` | `AUTOAIR_UPDATE_GOLDEN=1 uv run pytest tests/integration/test_airflow_dags.py -k golden_source` |
| `frontend/src/lib/generated/*.gen.ts` | `uv run python scripts/gen_shared_constants.py` |
| `src/autoair/static/{plugin,standalone}/` | `cd frontend && npm run build` |
| `uv.lock`, `frontend/package-lock.json`, `website/package-lock.json` | `uv lock` / `uv add`, `npm install` |
| `.artifacts/` (local output, gitignored) | `scan_hardcoded.py --write`, `check:site`, visual runs, screenshots |

## Hooks

Wired in `.claude/settings.json`, scripts in `.claude/hooks/` (stdlib Python, all fail open):

- `guard_bash.py` (PreToolUse Bash) denies what harms other people's state: `pkill`/`killall`,
  killing shared-port listeners, `git clean -f`, `git reset --hard`, destructive Docker prunes
  and volume removal, `airflow db reset|clean`, deleting the shared Airflow home or venvs,
  shell writes into generated files. It asks before `git commit`/`push`/`stash`/`checkout`/
  `switch`/`restore`/`rebase`/`merge`, publishing, stopping containers, and starting the dev
  stack on shared ports.
- `guard_generated.py` (PreToolUse Edit/Write) denies hand edits of generated files.
- `format_file.py` (PostToolUse Edit/Write) formats only the edited file: ruff for `*.py`,
  prettier for `frontend/src` and `frontend/e2e`.
- `stop_regen_check.py` (Stop, SubagentStop) reminds each agent once of the regeneration steps
  its own edits need; state is kept per session and per subagent (`agent_id`) under
  `$TMPDIR/autoair-agent-hooks-<uid>/`.

When a guard asks or denies, do not work around it: tell the user what you wanted to run and
why. After changing a hook or `settings.json`, run `python3 .claude/hooks/test_hooks.py`; it
also checks that every hook command names an existing script, because a missing one breaks
tool calls in every running session. Personal overrides go in `.claude/settings.local.json`
and `CLAUDE.local.md` (both gitignored).
