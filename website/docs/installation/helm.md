---
title: Helm
description: Deploy AutoAir as an Airflow plugin with the official Apache Airflow Helm chart using deploy/helm/values-autoair.yaml, explained setting by setting.
sidebar_label: Helm
---

AutoAir does not ship its own chart. On Kubernetes it runs as a plugin inside the official [Apache Airflow Helm chart](https://airflow.apache.org/docs/helm-chart/stable/index.html) (version 1.22 or newer), with an Airflow image that contains AutoAir. `deploy/helm/values-autoair.yaml` is a ready-made values file for a cloud-native setup:

- AutoAir commits DAG specs and generated files to Git, in pull-request mode;
- Airflow's `GitDagBundle` reads the same repository;
- no shared volume is needed and the API server replicas are stateless.

For a standalone AutoAir Deployment next to Airflow, an architecture overview and git-sync variants, see [Kubernetes](/docs/deployment/kubernetes), [Helm plugin deployment](/docs/deployment/helm-plugin) and [Git bundles](/docs/deployment/git-bundles).

## 1. Build the image

Build the wheel and an Airflow image that contains it, with the providers your DAGs use:

```bash
make build                                            # frontend + dist/autoair-*.whl
docker build -f deploy/docker/Dockerfile \
  --build-arg AIRFLOW_VERSION=3.3.2 \
  -t registry.example.com/my-airflow-autoair:3.3.2 .
docker push registry.example.com/my-airflow-autoair:3.3.2
```

`deploy/docker/Dockerfile` installs `autoair[git,mcp,ai,ai-anthropic,ai-openai,format,...]` (`AUTOAIR_EXTRAS`) plus `apache-airflow-providers-git` (needed by `GitDagBundle`) and `apache-airflow-providers-common-messaging`, then precomputes the operator catalog with `autoair catalog build --output /opt/airflow/autoair-catalog.json` and points `AUTOAIR_CATALOG_FILE` at it. Every API server replica therefore starts with an identical palette and does not scan providers at startup. See [Airflow plugin](/docs/installation/plugin#into-a-derived-image).

Add to the `pip install` line whatever the values below rely on:

- `s3fs` (or `gcsfs`, `adlfs`) when `AUTOAIR_TEMPLATES_URL` / `AUTOAIR_AUDIT_URL` point at object storage;
- `postgres` and `collab` extras when you move state to PostgreSQL or enable live collaboration across replicas.

## 2. Create the secrets

```bash
kubectl create secret generic autoair-git --from-literal=token="$GITHUB_TOKEN"
kubectl create secret generic autoair-ai  --from-literal=api-key="$ANTHROPIC_API_KEY"
```

The Git token needs permission to push branches and open pull requests on the repository (a fine-grained token, or use a GitHub App: `AUTOAIR_STORAGE__GIT__GITHUB_APP_ID`, `AUTOAIR_STORAGE__GIT__GITHUB_INSTALLATION_ID`, `AUTOAIR_STORAGE__GIT__GITHUB_PRIVATE_KEY`). The Airflow connection `git_dags` used by the bundle must exist as well (for example through the chart's connections or a secrets backend).

## 3. Install

```bash
helm repo add apache-airflow https://airflow.apache.org
helm upgrade --install airflow apache-airflow/airflow \
  -f deploy/helm/values-autoair.yaml \
  -f my-site-values.yaml
```

Copy the file and change the repository, registry and names to yours; keep site-specific values (ingress, executor, database) in your own values file.

## The values explained

### Image and API server

```yaml
images:
  airflow:
    repository: registry.example.com/my-airflow-autoair
    tag: "3.3.2"

apiServer:
  replicas: 2
```

The custom image is used for every Airflow component. AutoAir runs in each API server pod; two replicas means two independent AutoAir processes behind the same Service.

### DAG bundle

```yaml
config:
  dag_processor:
    dag_bundle_config_list: >-
      [{"name": "dags", "classpath": "airflow.providers.git.bundles.git.GitDagBundle",
        "kwargs": {"git_conn_id": "git_dags", "tracking_ref": "main", "subdir": "dags",
                   "refresh_interval": 60}}]
```

Airflow's dag-processor reads DAGs from the `main` branch of the repository behind the `git_dags` connection, sub-folder `dags`, refreshing every 60 seconds. Runs are pinned to the commit they were created from (`bundle_version`). A merged pull request therefore reaches Airflow within about a minute.

### Deploy target

| Variable | Value | Meaning |
| --- | --- | --- |
| `AUTOAIR_STORAGE__MODE` | `git` | Deploys commit to a Git repository |
| `AUTOAIR_STORAGE__GIT__PROVIDER` | `github` | Use GitHub's API (also `gitlab`, `bitbucket`, `azure`, `gitea` for Gitea/Forgejo, or `local` for the git CLI) |
| `AUTOAIR_STORAGE__GIT__REPO` | `acme/airflow-dags` | `owner/name` on GitHub |
| `AUTOAIR_STORAGE__GIT__BASE_BRANCH` | `main` | Branch pull requests target, the one the bundle tracks |
| `AUTOAIR_STORAGE__GIT__SUBDIR` | `dags/autoair` | Folder inside the repository; it is under the bundle's `subdir` (`dags`) so Airflow sees the files |
| `AUTOAIR_STORAGE__GIT__MODE` | `pr` | Each deploy opens (or updates) a branch `autoair/<dag_id>` and a pull request; `push` commits directly |
| `AUTOAIR_STORAGE__GIT__USE_AIRFLOW_BUNDLE` | `true` | Fill repository, branch, sub-folder and credentials from the `GitDagBundle` configuration where not set explicitly |

`AUTOAIR_STORAGE__GIT__TOKEN` comes from the `autoair-git` secret (see `secret:` below).

### State on object storage

| Variable | Value | Meaning |
| --- | --- | --- |
| `AUTOAIR_TEMPLATES_URL` | `s3://acme-airflow/autoair/templates` | Saved templates, shared by both replicas |
| `AUTOAIR_AUDIT_URL` | `s3://acme-airflow/autoair/audit` | Audit trail, shared by both replicas |

These are fsspec URLs: the image needs `s3fs` and the pods need S3 credentials (for example an IAM role for the service account). Other state (deploy proposals, AI memory, the deploy target chosen in the UI) stays in the per-pod state folder unless you configure it too. With `AUTOAIR_IAM__REQUIRE_APPROVAL=true` and several replicas, set `AUTOAIR_IAM__PROPOSALS_URL` to shared storage, or, simpler, move all state to PostgreSQL:

```yaml
env:
  - { name: AUTOAIR_DATABASE__BACKEND, value: "postgresql" }
  - { name: AUTOAIR_DATABASE__URL, value: "postgresql+psycopg://autoair@autoair-db:5432/autoair" }
  - { name: AUTOAIR_DATABASE__MIGRATE_ON_START, value: "false" }
secret:
  - { envName: AUTOAIR_DATABASE__PASSWORD, secretName: autoair-db, secretKey: password }
```

and run `autoair db upgrade` once per release (for example as a Helm pre-upgrade Job using the same image). See [Database](/docs/admin/database) and [PostgreSQL](/docs/deployment/postgres).

### Organisation policy

| Variable | Value | Meaning |
| --- | --- | --- |
| `AUTOAIR_REQUIRE_OWNER` | `true` | Every task must have an owner |
| `AUTOAIR_MIN_RETRIES` | `1` | Every task must retry at least once |
| `AUTOAIR_ENVIRONMENTS` | `["dev","prod"]` | Known environments for overlays and promotion |
| `AUTOAIR_DEFAULT_ENVIRONMENT` | `prod` | Environment preselected in the UI |

Violations are validation errors and block deploys. See [Policy](/docs/admin/policy) and [Environments](/docs/user-guide/environments).

### IAM

| Variable | Value | Meaning |
| --- | --- | --- |
| `AUTOAIR_IAM__STRATEGIES` | `["airflow_permissions","role_mapping","custom_view"]` | A user's AutoAir roles are the union of: Airflow DAG/config permissions, Airflow role names mapped to AutoAir roles, and AutoAir permissions defined in the auth manager |
| `AUTOAIR_IAM__DIRECT_PUSH_ROLES` | `["Admin"]` | Only Admins may skip the pull request and push directly |
| `AUTOAIR_IAM__REQUIRE_APPROVAL` | `true` | Four-eyes rule: going live needs another reviewer's approval of the exact version (pull requests are exempt) |

With the FAB auth manager run `autoair iam sync-fab --create-roles` once (for example as a Job) so that the `custom_view` permissions and `AutoAir <Role>` roles exist. See [IAM](/docs/admin/iam).

### Features, AI and configuration exposure

| Variable | Value | Meaning |
| --- | --- | --- |
| `AUTOAIR_FEATURES__MARKETPLACE` | `false` | Hide template marketplaces (all features are on by default) |
| `AUTOAIR_AI__PROVIDER` | `anthropic` | LLM provider |
| `AUTOAIR_AI__MODEL` | `claude-sonnet-5-5` | Model id |
| `AUTOAIR_AI__LLM_MODE` | `central` | Everyone uses the central model (`per_user` and `both` let users bring their own) |
| `AUTOAIR_AI__SERVICE_URL` | commented out | Proxy `/api/ai/*` to a separate AI service (`autoair ai-serve`) |
| `AUTOAIR_EXPOSE_CONFIG` | `non-sensitive-only` | Admins can read the effective configuration without secrets, like Airflow's `[api] expose_config` |

### Secrets

```yaml
secret:
  - envName: AUTOAIR_STORAGE__GIT__TOKEN
    secretName: autoair-git
    secretKey: token
  - envName: AUTOAIR_AI__API_KEY
    secretName: autoair-ai
    secretKey: api-key
  # - envName: AUTOAIR_AI__SERVICE__SHARED_SECRET
  #   secretName: autoair-ai
  #   secretKey: service-secret
```

The chart turns each entry into an environment variable from a Kubernetes Secret. `AUTOAIR_AI__API_KEY` is optional: without it AI features stay off while rule-based review keeps working. `AUTOAIR_AI__SERVICE__SHARED_SECRET` is required only when the AI service runs separately; it signs the forwarded caller identity.

## Verify

```bash
kubectl exec deploy/airflow-api-server -- airflow plugins | grep autoair
kubectl exec deploy/airflow-api-server -- autoair config show --changed
kubectl port-forward svc/airflow-api-server 8080:8080
curl -s localhost:8080/autoair/api/health
```

Then open **Browse → DAG Builder**, deploy a test DAG, and check that a pull request appears in the repository. After merging, the DAG shows up in Airflow within the bundle's `refresh_interval`.

The chart names the API server Deployment and Service `<release>-api-server`, so `deploy/airflow-api-server` and `svc/airflow-api-server` above assume the release name `airflow` used in the install command. With `useStandardNaming: true` the prefix becomes `fullnameOverride`, or `<release>-airflow` when the release name does not contain `airflow`.

## Upgrading

Build a new image with the new wheel, bump `images.airflow.tag`, and `helm upgrade`. Run `autoair db upgrade` first when state is in PostgreSQL. See [Upgrading](/docs/installation/upgrading).
