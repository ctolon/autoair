---
title: Upgrading
description: Upgrade AutoAir in each run mode - the wheel, state database migrations, the operator catalog, frontend assets, compatibility checks and rollback.
sidebar_label: Upgrading
---

An AutoAir upgrade touches four things: the Python package (which includes the frontend), the state database schema (SQL backends only), the operator catalog cache, and the browsers that have the old UI loaded. Deployed DAG files are not touched.

## Before you upgrade

1. **Read the release notes** for changed defaults and removed settings. Check your configuration against the new version with `autoair config show --changed` after installing it in a test environment; invalid settings stop the CLI with exit code 2 and make the plugin log `AutoAir: invalid configuration`.
2. **Check Airflow compatibility.** AutoAir supports Airflow 3.0 and newer below 4. When you upgrade Airflow at the same time, rebuild the catalog (below) and review [validation](/docs/user-guide/validation) results: features are gated by the installed Airflow version, or by `AUTOAIR_TARGET_AIRFLOW_VERSION` when set.
3. **Back up state.** With `database.backend=postgresql` or `sqlite`, back up the database; with the file backend, back up the state folder (`<dags_folder>/<subfolder>/.autoair` by default) and any `*_url` locations you configured. Specs live in the deploy target (folder, bucket or Git). See [Backup and restore](/docs/admin/backup-restore).

## Upgrade the package

### Local mode

```bash
uvx --refresh --from 'autoair[standalone,airflow]' autoair local     # newest release
uvx --from 'autoair[standalone,airflow]==<version>' autoair local     # a specific version
pip install -U 'autoair[standalone,airflow]'                          # in a virtual environment
```

### Standalone server

```bash
pip install -U 'autoair[standalone,format,git,mcp,collab,postgres]==<version>'
autoair db upgrade          # SQL backends, see below
# restart the service
```

With several replicas, run the migration once, then roll the replicas.

### Airflow plugin

Rebuild the Airflow image with the new wheel (and, when you upgrade Airflow too, the new base image), then roll out the API server:

```bash
make build
docker build -f deploy/docker/Dockerfile --build-arg AIRFLOW_VERSION=3.3.2 \
  -t registry.example.com/my-airflow-autoair:3.3.2-autoair<version> .
```

With Helm, bump `images.airflow.tag` and `helm upgrade`. Installing into a running virtual environment works too (`pip install -U "autoair[<extras>]" "apache-airflow==$(airflow version)"`, with the extras you installed before), followed by a restart of the API server; pinning Airflow keeps pip from changing it.

## Migrate the state database

The file backend has no schema; `autoair db upgrade` reports `database.backend is 'file': nothing to migrate` and exits with code 2.

For SQLite and PostgreSQL:

```bash
autoair db current          # <masked url>: version None (head 1)
                            # pending: [1] - run `autoair db upgrade`
autoair db upgrade          # applied: [1]; head: 1
autoair db current --json   # for scripts
```

- `autoair db upgrade` takes a database lock, so it is safe to run from several replicas at once; it only applies missing versions.
- `--revision N` stops at schema version `N` (it cannot go below the applied version).
- With `database.migrate_on_start=true` (the default) each server migrates on startup. For several replicas set it to `false` and run `autoair db upgrade` from an init container or a Helm pre-upgrade Job, as the Compose demo does with its `init-state` service.
- Administrators can see the schema version and pending migrations in the admin panel, and apply them from there.

The command reads the same configuration as the server (`AUTOAIR_DATABASE__BACKEND`, `AUTOAIR_DATABASE__URL`, `AUTOAIR_DATABASE__PASSWORD` and the other [`database`](/docs/reference/configuration#database) options, environment variables or configuration files), so run it with the server's environment.

## Rebuild the operator catalog

The catalog cache is keyed by the installed `apache-airflow*` distributions, the AutoAir version, the catalog code and the discovery settings, so an upgrade never serves a stale on-disk cache: the first start after an upgrade rebuilds it (in `AUTOAIR_CACHE_DIR`, default `$XDG_CACHE_HOME/autoair`).

A **precomputed** catalog file (`AUTOAIR_CATALOG_FILE`) built by an older version is detected as stale and ignored, with the log line `catalog file /opt/airflow/autoair-catalog.json is missing or stale; ignoring it`; AutoAir then builds the catalog at startup, which is slow with many providers. Rebuild it as part of the image build:

```bash
autoair catalog build --output /opt/airflow/autoair-catalog.json
# wrote /opt/airflow/autoair-catalog.json: 1234 operators, 87 providers, 0 errors
```

Do the same whenever you add or upgrade providers. Users with the `catalog:refresh` permission can also trigger a catalog refresh through the API.

## Frontend assets

The UI is part of the wheel; there is nothing to deploy separately. Content-hashed files are served with `Cache-Control: public, max-age=<server.static_max_age>, immutable`; entry files (`/ui/` pages and the plugin's `/autoair/static/main.js`) are served with `no-cache`, so browsers pick up the new UI on the next load.

A browser tab opened before the upgrade may still reference chunks that the new build no longer contains; reload the page if a dialog fails to open after an upgrade. During a rolling update, old and new pods serve different builds; prefer short rollouts or session affinity if users report missing chunks.

A wheel built from source without `npm run build` contains no UI and `/ui/` answers `503 Frontend bundle not built`. Released wheels always contain it.

## Specs and generated files

- **Specs** are migrated in memory when read (`spec_version` 1 to 2). They are written in the new format the next time the DAG is deployed. A spec written by a newer AutoAir than the one reading it is rejected (`spec_version N is newer than this AutoAir supports`).
- **Generated DAG files** are not rewritten by the upgrade. Changes in code generation (formatting, imports for a newer Airflow) appear the next time each DAG is deployed; the version diff shows them. Loader-mode DAGs build from the spec at parse time with the `autoair` installed on the dag-processor, so upgrade it there at the same time.

## Rollback

1. Reinstall the previous package version (or roll back the image tag / Helm release).
2. **Database**: migrations only go forward; there is no downgrade command. Restore the backup taken before the upgrade if the new version applied migrations.
3. **Specs** saved by the newer version keep `spec_version` 2 unless the release notes say otherwise; an older AutoAir that does not know a newer spec version refuses to open such specs.
4. **Catalog**: the cache key includes the version, so the previous version rebuilds or reuses its own cache; rebuild a precomputed `AUTOAIR_CATALOG_FILE` with the previous version.
5. Ask users to reload the UI.

### Older AutoAir against a newer database schema

Whether a rolled-back AutoAir starts against a database that a newer version already migrated depends on `database.migrate_on_start`:

| `database.migrate_on_start` | Result |
|---|---|
| `true` (default) | The migration step finds nothing to apply (every known migration is already recorded) and the server starts. It then runs against tables that may contain columns or tables it does not know; nothing checks that the schema is not newer. |
| `false` | Startup fails with ``database schema is at version <N>, AutoAir needs <M>: run `autoair db upgrade` ``, because the recorded version differs from the version this AutoAir expects. Running `autoair db upgrade` with the older version does not downgrade anything. |

Restoring the pre-upgrade backup (step 2) is the only supported way back to an older schema.

## Related

- [CLI reference](/docs/reference/cli)
- [Database](/docs/admin/database)
- [Scaling](/docs/admin/scaling)
- [Helm](/docs/installation/helm)
