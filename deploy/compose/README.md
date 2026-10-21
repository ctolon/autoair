# AutoAir demo stack (Docker Compose)

A complete local installation for evaluating AutoAir: Airflow 3 split into its real components,
the AutoAir plugin inside Airflow, two standalone AutoAir replicas sharing PostgreSQL state, and
opt-in profiles for a load balancer, Redis, the separate AI service, a local LLM, a Gitea deploy
target, TLS and tracing.

> **Workstation only.** Every Airflow visitor is an administrator (SimpleAuthManager
> `all_admins`), every secret has a public default, and ports bind to `127.0.0.1`. Use a real
> auth manager, unique secrets, TLS and the IAM role mapping before exposing anything.

The default start runs no AI provider, so it cannot incur provider charges.

## Contents

- [Quick start](#quick-start)
- [Services](#services)
- [Profiles](#profiles)
- [Ports and credentials](#ports-and-credentials)
- [Configuration](#configuration)
- [Walkthroughs](#walkthroughs)
- [Troubleshooting](#troubleshooting)
- [Inspect, stop and tear down](#inspect-stop-and-tear-down)
- [Single-container setup](#single-container-setup)

## Quick start

```bash
cd deploy/compose
cp .env.example .env                       # optional: every value has a default
docker compose -f docker-compose.demo.yml up --build -d --wait
```

The first build compiles both editor bundles and a wheel from the working tree. It takes a few
minutes; later starts take about a minute. When `--wait` returns:

| Open | URL |
| --- | --- |
| Airflow, then **Browse → DAG Builder** (the plugin) | http://localhost:8080 |
| Plugin, direct | http://localhost:8080/plugin/dag-builder |
| Standalone replica A | http://localhost:8765/ui/ |
| Standalone replica B | http://localhost:8766/ui/ |
| AutoAir API docs (replica A) | http://localhost:8765/api/docs |

Two demo DAGs, `demo_hello` and `demo_pipeline`, are deployed through replica A's API on
first start (`seed-dags`), and the signed template marketplace from `examples/marketplace` is
available in the template gallery.

Commands below assume the working directory is `deploy/compose`. From the repository root, use
`-f deploy/compose/docker-compose.demo.yml` and give `--env-file` paths with the same prefix.

## Services

### Core (always started)

| Service | Image | Role | Health check |
| --- | --- | --- | --- |
| `postgres` | `postgres:17-alpine` | Two roles and databases from `initdb/001-databases.sh`: `airflow` (Airflow metadata) and `autoair` (AutoAir state) | `pg_isready` for both databases |
| `airflow-migrate` | demo image | One-shot `airflow db migrate` | exits 0 |
| `autoair-migrate` | demo image | One-shot `autoair db upgrade`. Replicas run with `database.migrate_on_start=false` | exits 0 |
| `marketplace-seed` | demo image | One-shot: signs `examples/marketplace` with a generated Ed25519 key into the `marketplace` volume | exits 0 |
| `airflow-apiserver` | demo image | Airflow UI and REST API, plus the AutoAir plugin (`/autoair/...`) and the execution API | `/api/v2/monitor/health` |
| `airflow-scheduler` | demo image | Scheduler with the LocalExecutor (runs the tasks) | scheduler health server `:8974/health` |
| `airflow-dag-processor` | demo image | Parses the DAG bundles | `airflow jobs check --job-type DagProcessorJob` |
| `airflow-triggerer` | demo image | Deferrable operators | `airflow jobs check --job-type TriggererJob` |
| `autoair-a`, `autoair-b` | demo image | `autoair serve` replicas sharing the `autoair` database and the `dags` volume | `/api/health` (readiness: `/api/ready`) |
| `seed-dags` | demo image | One-shot: deploys `seed/specs/*.json` through replica A (skips DAGs that already exist) | exits 0 |

Start order is enforced with `depends_on` conditions: PostgreSQL healthy, then both
migrations complete, then Airflow, then the replicas, then the seed. With profile `git`,
`git-bootstrap` completes before any Airflow component or replica starts, because the
GitDagBundle and AutoAir's git target read the Airflow connection at startup.

The "demo image" is built from `deploy/docker/Dockerfile.demo`: `apache/airflow:3.3.2` plus the
AutoAir wheel from the working tree with the `standalone, format, git, collab, collab-redis,
postgres, otel, yaml` extras and the `standard` and `git` providers.

### Volumes

| Volume | Content |
| --- | --- |
| `postgres` | Both databases |
| `dags` | Airflow's `dags_folder`; the local deploy target writes to `dags/autoair/` |
| `logs` | Task logs (scheduler writes, api-server reads) |
| `marketplace` | Signed example marketplace, its key pair and `sources.json` |
| `gitea-data`, `gitea-config` | Profile `git` |
| `certs` | Profile `tls`: demo CA and server certificate |
| `ollama` | Profile `ollama`: downloaded models |

## Profiles

Profiles add services. When a profile also needs the core services configured differently,
a preset in `env/` sets the matching `DEMO_*` values. Pass the preset as a second
`--env-file` after `.env`; the later file wins.

| Profile | Adds | Preset | Purpose |
| --- | --- | --- | --- |
| `lb` | `lb` (nginx) | none | Round-robin over both replicas, WebSockets and SSE included, no sticky sessions |
| `redis` | `redis` | `env/redis.env` | Collaboration fan-out through Redis pub/sub instead of PostgreSQL `LISTEN/NOTIFY` |
| `ai` | `autoair-ai` | `env/ai.env` (hosted provider) or `env/ollama.env` | `autoair ai-serve` as a separate service. Replicas and the plugin proxy `/api/ai/*` to it, signing the caller with the HMAC secret |
| `ollama` | `ollama`, `ollama-pull` | `env/ollama.env` | Local model server and a one-shot model pull. No provider charges |
| `git` | `gitea`, `gitea-init`, `git-bootstrap` | `env/git.env` | Gitea as the deploy target in PR mode, read back by an Airflow `GitDagBundle` |
| `tls` | `certs-init`, `tls-proxy`, `autoair-tls` | none | Self-signed CA, an HTTPS proxy, and a replica serving HTTPS itself that reaches Airflow over verified HTTPS |
| `observability` | `otel-collector`, `jaeger` | `env/observability.env` | OpenTelemetry traces from every AutoAir process, viewed in Jaeger |
| `kb-pgvector` | `kb-pgvector` (needs `-f docker-compose.kb.yml`) | `env/kb-pgvector.env` | The documentation knowledge base in PostgreSQL with pgvector |
| `kb-elasticsearch` | `kb-elasticsearch` (needs `-f docker-compose.kb.yml`) | `env/kb-elasticsearch.env` | The documentation knowledge base in a single-node Elasticsearch |

```bash
# one profile
docker compose -f docker-compose.demo.yml --profile lb up -d --wait

# profile + preset
docker compose -f docker-compose.demo.yml --env-file .env --env-file env/git.env \
  --profile git up -d --wait

# several at once
docker compose -f docker-compose.demo.yml --env-file .env \
  --env-file env/git.env --env-file env/redis.env --env-file env/observability.env \
  --profile git --profile redis --profile lb --profile observability up -d --wait
```

`.env` must exist when it is named with `--env-file`. Run `cp .env.example .env` first; an
all-comments `.env` is fine. Keep using the same `--env-file` and `--profile` flags for later
`ps`, `logs` and `down` commands, or put them in `.env` as `COMPOSE_PROFILES=git,redis`.

The AI service and the observability exporters are inert without their presets. Setting
`DEMO_AI_SERVICE_URL` without starting profile `ai` makes `/api/ai/*` answer with proxy
errors. The rest keeps working.

## Ports and credentials

All ports bind to `127.0.0.1`. Change any of them in `.env` to avoid collisions.

| Variable | Default | Service |
| --- | --- | --- |
| `DEMO_AIRFLOW_PORT` | 8080 | Airflow + plugin |
| `DEMO_AUTOAIR_A_PORT` | 8765 | replica A |
| `DEMO_AUTOAIR_B_PORT` | 8766 | replica B |
| `DEMO_POSTGRES_PORT` | 5433 | PostgreSQL |
| `DEMO_LB_PORT` | 8780 | `lb` |
| `DEMO_AI_PORT` | 8790 | `autoair-ai` |
| `DEMO_OLLAMA_PORT` | 11434 | `ollama` |
| `DEMO_GITEA_PORT` | 3000 | `gitea` |
| `DEMO_TLS_PORT` | 8443 | `tls-proxy` → replicas (HTTPS) |
| `DEMO_TLS_AIRFLOW_PORT` | 9443 | `tls-proxy` → Airflow (HTTPS) |
| `DEMO_TLS_AUTOAIR_PORT` | 8445 | `autoair-tls` (HTTPS from uvicorn) |
| `DEMO_JAEGER_PORT` | 16686 | Jaeger UI |

| What | Credentials |
| --- | --- |
| Airflow UI | none: SimpleAuthManager `all_admins`, everybody is `Anonymous` with role `ADMIN` |
| Airflow API token | `curl -s localhost:8080/auth/token` (works only in `all_admins` mode) |
| AutoAir replicas | **Sign in with Airflow** (Airflow's session cookie is sent to every port on `localhost`), or paste the token above |
| PostgreSQL | `postgres` / `postgres-demo-only`; `airflow` / `airflow-demo-only`; `autoair` / `autoair-demo-only` |
| pgvector (profile `kb-pgvector`) | `autoair_kb` / `kb-demo-only` |
| Gitea (profile `git`) | `autoair` / `autoair-demo-only` |
| Redis (profile `redis`) | password `redis-demo-only` |
| AI service HMAC | `DEMO_AI_SERVICE_SECRET` (`local-demo-ai-hmac-secret`) |
| Gitea access token | Created by `git-bootstrap` and stored **only** in the Airflow connection `gitea_dags` (Fernet-encrypted with `DEMO_FERNET_KEY`) |

## Configuration

There are three layers, highest precedence first:

1. **`docker-compose.demo.yml` environment**, driven by `DEMO_*` variables from `.env` and the
   presets. `.env.example` lists every variable with its default and a comment.
2. **`autoair.env`** (optional, git-ignored): any extra `AUTOAIR_*` variable for every AutoAir
   container, without editing the compose file, for example
   `AUTOAIR_IAM__REQUIRE_APPROVAL=true` or `AUTOAIR_UI__BANNER_TEXT=Staging`.
3. **`config/autoair.yaml`**, mounted at `/etc/autoair/autoair.yaml` (`AUTOAIR_CONFIG_FILE`) in
   the plugin, the replicas and the AI service. It shows:
   - feature flags (`features.*`);
   - IAM: strategies, Airflow role to AutoAir role mapping, the `AutoAir ` role prefix, a
     custom role, direct-push roles and the four-eyes switch;
   - policy rules: dag/task id patterns, required description, max tasks, privileged and
     secret-parameter patterns;
   - governance: commit, PR title and PR body templates, PR labels, commit author;
   - UI branding: the orange demo banner.

Check the effective values and where each one comes from:

```bash
docker compose -f docker-compose.demo.yml exec autoair-a autoair config show --changed
```

The full reference is in `docs/configuration-reference.md`. The demo sets
`expose_config=non-sensitive-only`, so administrators can also read it at `GET /api/config`.

Changes to `config/autoair.yaml` and `autoair.env` take effect on restart, for example
`docker compose -f docker-compose.demo.yml restart autoair-a autoair-b airflow-apiserver`.
Changes to `.env` need `up -d` so that Compose recreates the affected containers.

## Walkthroughs

### 1. Build, validate and deploy in the plugin

1. Open http://localhost:8080/plugin/dag-builder. The banner comes from `config/autoair.yaml`.
2. Drag an **EmptyOperator** and a **BashOperator** onto the canvas, connect them, and set
   `bash_command` to `echo hello`.
3. The Problems tab shows the policy at work: without a description the deploy is blocked
   (`require_description: true`). A dag id with capitals breaks `policy.dag_id_pattern`.
4. Add a description, then **Deploy**. The file appears as `dags/autoair/<dag_id>.py`.
5. Find the DAG in Airflow, unpause it, trigger it and read the task log.

### 2. Two replicas, one state

1. Sign in on http://localhost:8765/ui/ and http://localhost:8766/ui/ with **Sign in with
   Airflow**.
2. Open `demo_pipeline` on both. Click **Collaborate** in both browsers and edit different
   task parameters. Both converge through PostgreSQL `LISTEN/NOTIFY`, or through Redis with
   profile `redis`.
3. Save an operator template on A and open it from B. In **Admin**, the audit trail and the
   replica list are the same on both.

### 3. Load balancer (`lb`)

```bash
docker compose -f docker-compose.demo.yml --profile lb up -d --wait
curl -sI localhost:8780/api/health | grep X-Upstream
```

Use http://localhost:8780/ui/. No sticky sessions are configured. Each response carries
`X-Upstream` with the replica that answered. nginx balances per worker process, so a series of
one-off `curl` calls can land on the same replica. Requests over one keep-alive connection
alternate.

### 4. Git deploy target with pull requests (`git`)

```bash
docker compose -f docker-compose.demo.yml --env-file .env --env-file env/git.env \
  --profile git up -d --wait
```

What happens:

1. `gitea-init` creates the admin user `autoair`.
2. `git-bootstrap` creates the private repository `autoair/dags` with a `main` branch and an
   access token. It stores url, user and token in the Airflow connection `gitea_dags` and
   never prints the token. On later `up` runs it keeps a token that still works.
3. Airflow's dag-processor reads two bundles: `dags-folder` (the local volume) and `gitea`, a
   `GitDagBundle` on `main` with `git_conn_id=gitea_dags`.
4. AutoAir runs with `storage.mode=git`, `storage.git.use_airflow_bundle=true` and
   `bundle_name=gitea`. Repository, branch and credentials come from the bundle and its
   connection. AutoAir stores no credentials.
5. AutoAir uses `storage.git.provider=gitea`. In PR mode every deploy pushes the branch
   `autoair/<dag_id>` with git and opens a pull request into `main` through Gitea's REST API,
   with the connection's token. Each commit and pull request carries the governance context
   block, and each commit the `X-AutoAir-*` trailers. The seed DAGs arrive as two pull
   requests.

Merge a deploy:

1. Open http://localhost:3000/autoair/dags/pulls (`autoair` / `autoair-demo-only`). The
   deploy result in AutoAir links to the pull request as well.
2. Open the pull request of `autoair/demo_hello` and merge it.
3. Within `refresh_interval` (30 s) the DAG shows up in Airflow with bundle `gitea`. Its runs
   are pinned to the merged commit SHA (`bundle_version`).

Pull requests are opened through the `gitea` provider, which also works with Forgejo.
GitHub, GitLab, Bitbucket and Azure DevOps have their own providers: set
`storage.git.provider`, `repo` and `api_url`, and see [Git bundles](https://ctolon.github.io/autoair/docs/deployment/git-bundles).
`DEMO_GIT_MODE=push` commits straight to `main` instead.

The **Versions & Git → Deploy target…** dialog lists the `gitea` bundle as well. You can
switch between the local folder and the git target there without a restart.

### 5. Separate AI service (`ai`, `ollama`)

Local model, no provider charges:

```bash
docker compose -f docker-compose.demo.yml --env-file .env --env-file env/ollama.env \
  --profile ai --profile ollama up -d --wait
```

`ollama-pull` downloads `DEMO_OLLAMA_MODEL` (default `qwen2.5:1.5b`, about 1 GB) before
`autoair-ai` starts. CPU inference is slow; expect tens of seconds per answer.

Hosted provider (billed per token): set `DEMO_AI_PROVIDER` and `DEMO_AI_API_KEY` in `.env`,
then:

```bash
docker compose -f docker-compose.demo.yml --env-file .env --env-file env/ai.env \
  --profile ai up -d --wait
```

The replicas and the plugin set `AUTOAIR_AI__SERVICE_URL=http://autoair-ai:8790` and proxy
`/api/ai/*` there. The proxy signs the caller's identity with
`AUTOAIR_AI__SERVICE__SHARED_SECRET`, and the service trusts only signed requests. Its health
check is http://localhost:8790/health.

### 6. TLS (`tls`)

```bash
docker compose -f docker-compose.demo.yml --profile tls up -d --wait
docker compose -f docker-compose.demo.yml --profile tls run --rm --no-deps --entrypoint cat \
  certs-init /certs/ca.pem > demo-ca.pem
curl --cacert demo-ca.pem https://localhost:8443/api/health       # proxy -> replicas
curl --cacert demo-ca.pem https://localhost:9443/api/v2/version   # proxy -> Airflow
curl --cacert demo-ca.pem -I https://localhost:8445/api/health    # uvicorn TLS, HSTS header
```

- `certs-init` creates a 30-day demo CA and a server certificate for `localhost`,
  `tls-proxy`, `autoair-tls`, `autoair-a`, `autoair-b` and `127.0.0.1`. The certificate
  includes the key identifiers that Python 3.13's strict verification needs.
- `autoair-tls` serves HTTPS itself (`server.ssl_certfile`, `ssl_keyfile`, `ssl_min_version`,
  `hsts_seconds`, `cookie_secure=always`). It calls Airflow at `https://tls-proxy:9443` and
  trusts only the demo CA (`outbound.ca_bundle`). Its `/api/ready` reporting `airflow: ok`
  proves the outbound TLS path.
- Browsers warn about the demo CA. Import `demo-ca.pem` or accept the warning.

### 7. Tracing (`observability`)

```bash
docker compose -f docker-compose.demo.yml --env-file .env --env-file env/observability.env \
  --profile observability up -d --wait
```

The preset sets `AUTOAIR_OTEL_ENABLED=true` on every AutoAir process. Spans go over OTLP/HTTP
to `otel-collector:4318`, which forwards them to Jaeger: http://localhost:16686 (services
`autoair-a`, `autoair-b`, `autoair`, the plugin inside Airflow, and `autoair-ai`). Health,
readiness and metrics URLs are not traced (`server.otel_excluded_urls`). Prometheus metrics
are always at `/api/metrics`.

### 8. Knowledge base stores (`kb-pgvector`, `kb-elasticsearch`)

The documentation knowledge base uses a local SQLite file by default. `docker-compose.kb.yml`
adds the two server stores as profiles and passes `AUTOAIR_KB__*` to the plugin and both
replicas:

```bash
# PostgreSQL with pgvector (pgvector/pgvector:pg17), read through the Airflow connection
# kb_pgvector (AIRFLOW_CONN_KB_PGVECTOR, environment backend)
docker compose -f docker-compose.demo.yml -f docker-compose.kb.yml \
  --env-file .env --env-file env/kb-pgvector.env --profile kb-pgvector up -d --wait

# Elasticsearch 9.5.4, single node, security off (workstation only), on 127.0.0.1:9200
docker compose -f docker-compose.demo.yml -f docker-compose.kb.yml \
  --env-file .env --env-file env/kb-elasticsearch.env --profile kb-elasticsearch up -d --wait
```

The index starts empty because `kb.online` is off in the demo. Set `DEMO_KB_ONLINE=true` in
`.env` to let the replicas fetch airflow.apache.org and astronomer.io (the first build takes a
few minutes), or copy a bundle into a replica and run `autoair kb import <file>` there. Check
the index with:

```bash
docker compose -f docker-compose.demo.yml -f docker-compose.kb.yml \
  --env-file .env --env-file env/kb-pgvector.env --profile kb-pgvector \
  exec autoair-a autoair kb status
```

Then open **Admin -> Knowledge base** on a replica. Variables: `DEMO_KB_STORE`,
`DEMO_KB_ONLINE`, `DEMO_KB_PGVECTOR_CONN_ID`, `DEMO_KB_PG_PASSWORD` (`kb-demo-only`),
`DEMO_KB_ELASTICSEARCH_URL`, `DEMO_KB_ES_PORT` (9200). Elasticsearch needs about 1 GiB of memory.

### 9. Isolated import checks (validator service)

`docker-compose.validator.yml` adds `autoair-validator` (`autoair validator-serve`) and points
the plugin and both replicas at it, so the DagBag import check no longer executes DAG code
inside the Airflow api-server or the replicas (Python DAG import and test runs still do):

```bash
docker compose -f docker-compose.demo.yml -f docker-compose.validator.yml up -d --wait
docker compose -f docker-compose.demo.yml -f docker-compose.validator.yml \
  exec autoair-a python -c "import urllib.request; print(urllib.request.urlopen('http://autoair-validator:8791/ready').read().decode())"
```

The validator runs read-only, without capabilities, as uid 50000, with memory, CPU and process
limits, on an internal network without a route to the internet (it can still reach the
plugin and the replicas on that network; the Helm chart's NetworkPolicy denies all egress). Stop it
(`docker compose ... stop autoair-validator`) and a deploy goes ahead with the warning "the import
check service is unavailable ... the DAG was not imported (fail open)";
`DEMO_VALIDATOR_FAIL_MODE=closed` blocks deploys instead, `local` checks in the main server. Variables: `DEMO_VALIDATOR_KEY` (the shared HMAC key, public default),
`DEMO_VALIDATOR_FAIL_MODE` (`open`), `DEMO_VALIDATOR_POOL_SIZE` (2).

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `port is already allocated` | Another service uses the port. Set the `DEMO_*_PORT` variables in `.env` |
| `all predefined address pools have been fully subnetted` | Docker has no free network ranges. Remove unused networks (`docker network prune` affects every project), or add an override file that pins the default network to a free subnet with `networks.default.ipam.config[0].subnet` |
| `postgres` stays unhealthy, or `password authentication failed` | The volume was initialised with other passwords. Initialisation runs only once: `down -v` or `ALTER ROLE` |
| `autoair-migrate` fails | `docker compose logs autoair-migrate`. Usually the database password does not match the volume |
| Plugin page empty, `main.js` returns HTML | The AutoAir plugin failed to initialise. See `docker compose logs airflow-apiserver \| grep -A20 "AutoAir plugin failed"`. With `git`, check that `git-bootstrap` succeeded before Airflow started |
| Git deploy: `Authentication failed` | AutoAir re-reads the Airflow connection after an authentication failure (and every `storage.git.credentials_ttl` seconds), so a rotated token is picked up on the next deploy. If it persists, the token in `gitea_dags` itself is wrong: rerun `git-bootstrap` |
| A merged git DAG does not appear | Wait for `refresh_interval` (30 s), then check `docker compose logs airflow-dag-processor`. The connection must be `git_conn_id` in `env/git.env` (`gitea_dags`) |
| `seed-dags` exited 1 | Its log lists the failing DAG and the reason. It is idempotent: `docker compose up seed-dags` |
| Replicas show the sign-in page | Normal in a new browser profile. Use **Sign in with Airflow** after opening Airflow once, or paste `curl -s localhost:8080/auth/token` |
| Marketplace shows nothing | `docker compose logs marketplace-seed`. The replicas read `sources.json` from the `marketplace` volume at startup |
| AI answers 503 | AI is off by default. Use profile `ai` with a preset. `/api/ai/*` errors with `DEMO_AI_SERVICE_URL` set mean `autoair-ai` is not running |
| Ollama answers very slowly | CPU inference. Pick a smaller `DEMO_OLLAMA_MODEL` |
| `x509: Missing Authority Key Identifier` | Certificates from an older `certs-init`. Remove the `certs` volume of this project and start again |
| Everything is slow to start the first time | The image build (Node and Python) and Airflow's first migration. Later starts reuse both |

Useful commands:

```bash
docker compose -f docker-compose.demo.yml ps -a                      # one-shot jobs included
docker compose -f docker-compose.demo.yml logs -f autoair-a
docker compose -f docker-compose.demo.yml exec autoair-a autoair db current
curl -s localhost:8765/api/ready
```

## Inspect, stop and tear down

```bash
docker compose -f docker-compose.demo.yml stop          # stop, keep containers and data
docker compose -f docker-compose.demo.yml down          # remove containers, keep volumes
docker compose -f docker-compose.demo.yml down -v       # also erase databases, DAGs, Gitea
```

Pass the same `--profile` and `--env-file` flags as for `up`. Without them, `down` leaves the
profile services running.

To run a second copy next to the first, for example to test a branch, use another project
name and other ports:

```bash
docker compose -p autoair-demo-2 -f docker-compose.demo.yml --env-file my-ports.env up -d --wait
docker compose -p autoair-demo-2 -f docker-compose.demo.yml down -v  # removes only that copy
```

## Single-container setup

`docker-compose.yaml` runs Airflow standalone with the plugin in one container and DAGs on a
volume. It needs a prebuilt wheel (`make build`).
