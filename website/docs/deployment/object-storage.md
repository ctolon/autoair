---
title: Object storage
description: Deploy AutoAir DAGs to an S3 or GCS prefix that Airflow reads through an object-storage DAG bundle, and keep AutoAir state on object storage.
sidebar_label: Object storage
---

The `object` deploy target writes DAG specs and generated files to an [fsspec](https://filesystem-spec.readthedocs.io/) URL such as `s3://bucket/dags/autoair/` or `gs://bucket/dags/autoair/`. Airflow reads the same prefix through an object-storage DAG bundle (for example the Amazon provider's `S3DagBundle`). API-server replicas need no shared volume and no git host.

Use it when your platform already distributes DAGs through a bucket and you do not need pull-request review. For review workflows and run-level versioning, prefer [git bundles](/docs/deployment/git-bundles).

## How it works

For each deploy AutoAir writes, under `storage.object_url`:

| Object | Content |
|---|---|
| `<dag_id>.autoair.json` | The spec, written first |
| `<dag_id>.py` (or the YAML file / loader stub, depending on the output mode) | The generated DAG, written last |
| `.autoair/history/<dag_id>/...` | Saved versions (at most `storage.history_limit`, default 100) with the deploy message |
| `.autoair/state/...` | Ownership markers used to detect hand-edited ("ejected") files |

Each object is written whole (one PUT; on a `file://` URL a temporary file renamed into place), but there is no transaction across objects. The spec is written first, then the DAG file, the history entry and the state record, so a bundle refresh between two writes can see the new spec with the old DAG file; the next refresh sees both. When a write fails, AutoAir restores the objects it already wrote, so the spec, its etag and the history do not advance without the DAG file.

Writers of one DAG are serialised by a write lease kept in AutoAir's state backend (collection `object_deploy_locks`): a save takes it, checks `base_etag` and the ownership records, writes, and releases it. A second replica that loaded the same version waits for the lease and then gets a conflict (409) instead of overwriting the first save. The lease is shared by every replica only on a SQL backend (`database.backend=sqlite` on a shared file, or `postgresql`); with the default `file` backend it is shared by the processes of one host. `storage.object_lock_seconds` (300) is how long a lease lasts, so a replica that dies while writing blocks that DAG at most that long; `storage.object_lock_wait_seconds` (30) is how long a save waits for it.

After a deploy AutoAir does not request a reparse (it does that only for the `local` target); Airflow picks the change up on the bundle's next refresh.

## Configuration

Install the fsspec implementation for your store in the image that runs AutoAir; AutoAir does not depend on it:

```bash
pip install s3fs        # s3://
pip install gcsfs       # gs://
pip install adlfs       # abfs:// (Azure Blob)
```

Then:

```bash
AUTOAIR_STORAGE__MODE=object
AUTOAIR_STORAGE__OBJECT_URL=s3://acme-airflow/dags/autoair/
AUTOAIR_STORAGE__STORAGE_OPTIONS='{"client_kwargs": {"region_name": "eu-central-1"}}'
```

| Setting | Default | Purpose |
|---|---|---|
| `storage.mode` | `local` | `object` selects this target |
| `storage.object_url` | none | fsspec URL of the bundle prefix; required (`storage mode 'object' needs object_url` otherwise) |
| `storage.storage_options` | `{}` | fsspec options passed to the filesystem (credentials, endpoint, region). Treated as secret: masked by `GET /api/config` |
| `storage.history_limit` | `100` | Versions kept per DAG |
| `storage.allowed_targets` | `["local", "git", "object"]` | Restrict what the deploy-target dialog may choose |

Prefer workload identity (IRSA on EKS, Workload Identity on GKE) or the standard environment variables (`AWS_*`, `GOOGLE_APPLICATION_CREDENTIALS`) over keys in `storage_options`. For S3-compatible stores such as MinIO, set the endpoint:

```bash
AUTOAIR_STORAGE__STORAGE_OPTIONS='{"endpoint_url": "https://minio.acme.io", "key": "...", "secret": "..."}'
```

The deploy-target API (`PUT /api/deploy-target`) never stores credentials: a target chosen at runtime is refused when it carries `storage_options` credentials that are not already configured (`***` keeps the configured value). Put credentials in `AUTOAIR_STORAGE__STORAGE_OPTIONS` or the environment.

## Choosing the target in the UI

Administrators (permission `admin:deploy_target`) can switch to object storage without a restart: **Versions & Git → Deploy target…**. The dialog lists:

- every `S3DagBundle` and `GCSDagBundle` in Airflow's `[dag_processor] dag_bundle_config_list`, as `s3://<bucket_name>/<prefix>` or `gs://<bucket_name>/<prefix>`. The bundle's `aws_conn_id` or `gcp_conn_id` is shown for reference only: AutoAir writes with its own fsspec credentials, not with that connection;
- **Object storage (S3, GCS, Azure Blob)**, an fsspec URL entered by hand plus optional storage options as JSON. When the active target is already object storage, the fields start from it, with credentials shown as `***`.

**Test** builds the target and lists it; **Save & use** switches to it. Only kinds listed in `storage.allowed_targets` are offered.

## Airflow side

Point a DAG bundle at the same prefix. With the Amazon provider:

```yaml
config:
  dag_processor:
    dag_bundle_config_list: >-
      [{"name": "autoair", "classpath": "airflow.providers.amazon.aws.bundles.s3.S3DagBundle",
        "kwargs": {"aws_conn_id": "aws_dags", "bucket_name": "acme-airflow",
                   "prefix": "dags/autoair", "refresh_interval": 60}}]
```

With the Google provider, the GCS bundle takes the same shape:

```yaml
config:
  dag_processor:
    dag_bundle_config_list: >-
      [{"name": "autoair", "classpath": "airflow.providers.google.cloud.bundles.gcs.GCSDagBundle",
        "kwargs": {"gcp_conn_id": "gcp_dags", "bucket_name": "acme-airflow",
                   "prefix": "dags/autoair", "refresh_interval": 60}}]
```

`bucket_name` is required; `aws_conn_id` / `gcp_conn_id` default to the provider's default connection, `prefix` to the bucket root, and `refresh_interval` is the generic bundle option. These are the constructor arguments of `S3DagBundle` and `GCSDagBundle` in the current Amazon and Google providers; AutoAir's tests do not run either bundle, so check them against the provider versions you install.

The bundle must cover `storage.object_url`; a separate bundle for AutoAir's prefix keeps hand-written DAGs apart. Object-storage bundles do not version DAGs the way `GitDagBundle` does, so runs do not carry a commit `bundle_version`.

## AutoAir state on object storage

Independently of the deploy target, the file backend of AutoAir's own state accepts fsspec URLs. This is how `deploy/helm/values-autoair.yaml` keeps templates and the audit trail across pod restarts:

```bash
AUTOAIR_TEMPLATES_URL=s3://acme-airflow/autoair/templates
AUTOAIR_AUDIT_URL=s3://acme-airflow/autoair/audit
AUTOAIR_ICONS_URL=s3://acme-airflow/autoair/icons
AUTOAIR_IAM__PROPOSALS_URL=s3://acme-airflow/autoair/proposals
AUTOAIR_AI__MEMORY_URL=s3://acme-airflow/autoair/ai-memory
AUTOAIR_AI__AUDIT__URL=s3://acme-airflow/autoair/ai-audit
AUTOAIR_AI__KNOWLEDGE__STORE_URL=s3://acme-airflow/autoair/ai-knowledge
AUTOAIR_AI__USER_LLM__STORE_URL=s3://acme-airflow/autoair/ai-users
```

Object stores give no locks: several replicas writing the same document follow last-writer-wins, and reviews written at the same instant can lose one. For more than one replica, [PostgreSQL](/docs/deployment/postgres) replaces all of these URLs with transactional storage. Note that PostgreSQL does not move the DAG specs and files: they always live in the deploy target.

## Failure behaviour

| Situation | Result |
|---|---|
| Bucket unreachable or credentials invalid | Deploy fails with the filesystem's error; `/api/ready` reports the storage check failed |
| Prefix not writable | `/api/ready` reports `storage: read-only` (503) |
| fsspec implementation missing | The target cannot be built: AutoAir starts degraded, `/api/ready` answers 503 with the error and deploys fail until `s3fs` / `gcsfs` is installed (see [Observability](/docs/admin/observability#degraded-startup)) |
| Object at the target path not created by AutoAir | Refused; foreign files are never overwritten |

## Related pages

- [Git bundles and git-sync](/docs/deployment/git-bundles)
- [State and scaling](/docs/admin/scaling)
- [`storage` settings](/docs/reference/configuration#storage)
