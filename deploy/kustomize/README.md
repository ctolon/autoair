# AutoAir on Kubernetes with Kustomize

Raw Kubernetes manifests for the standalone AutoAir server (`autoair serve`) and its optional
services, assembled with Kustomize (`kubectl kustomize` / `kubectl apply -k`, or the
`kustomize` binary). User documentation: the website page *Deployment -> Kubernetes with
Kustomize* (`website/docs/deployment/kustomize.md`).

Other paths: the plugin inside an Airflow installed with the official Helm chart uses
[`../helm/values-autoair.yaml`](../helm/values-autoair.yaml); the validator alone as a chart is
[`../helm/autoair-validator`](../helm/autoair-validator); a workstation stack is
[`../compose`](../compose/README.md).

```text
base/                 autoair serve: Deployment, Service, ServiceAccount, PDB, ConfigMap
components/
  postgresql/         state in PostgreSQL + `autoair db upgrade` init container
  git-target/         deploy target git (token from an Airflow connection)
  validator/          autoair validator-serve, NetworkPolicy without egress (AutoAir >= 0.2.0)
  ai-service/         autoair ai-serve, the server proxies /api/ai/* and /mcp to it
  mcp-http/           autoair mcp-serve --http (optional, the server already serves /mcp)
  ingress/            Ingress on its own host name + public URL settings (instead of gateway-api)
  gateway-api/        HTTPRoute on an existing Gateway + public URL settings (production default)
  hpa/                HorizontalPodAutoscaler 2-6 replicas (needs postgresql)
  network-policies/   policies for the server, AI service and MCP server
  service-monitor/    Prometheus Operator ServiceMonitors (+ a metrics NetworkPolicy)
  airflow-plugin/     plugin mode for an Airflow deployed from plain manifests
overlays/
  dev/                one replica, file state and DAGs on a PersistentVolumeClaim
  production/         postgresql, git, validator, gateway-api, HPA, PDB, NetworkPolicies
  airflow-plugin/     example: the airflow-plugin component on a stand-in api-server
validate.sh           builds everything and runs kubeconform (CI runs it)
```

## Quick start (dev)

```bash
# AUTOAIR_AIRFLOW_URL in overlays/dev/settings.env: your Airflow api-server
kubectl apply -k deploy/kustomize/overlays/dev
kubectl -n autoair-dev rollout status deploy/autoair
kubectl -n autoair-dev port-forward svc/autoair 8765:8765   # http://localhost:8765/ui/
```

## Production

1. Copy `overlays/production` into your configuration repository (keep the relative paths to
   `base` and `components`, or point them at a git URL of this repository with a `?ref=` tag).
2. Edit `overlays/production/settings/settings.env`: Airflow URLs, public URL, PostgreSQL URL,
   git repository. It is a Component listed last, so its values win over the defaults of the
   other components; a `configMapGenerator` in the overlay itself would run before the
   components and lose to them.
3. Point the HTTPRoute at your Gateway (`components/gateway-api/httproute.yaml`: `parentRefs`,
   `hostnames`; TLS terminates at the Gateway) — or, with `components/ingress`, patch the Ingress
   host and TLS Secret — and the namespace
   and pod selectors in `components/network-policies` and `components/validator`
   (the Gateway's namespace `gateway-system` or your ingress controller's, Airflow's namespace
   `airflow`, DNS).
4. Create the Secrets (below), then `kubectl apply -k overlays/production`.

The validator component runs `autoair validator-serve`, which exists from AutoAir 0.2.0: with
the 0.1.0 image drop it from the overlay.

## Secrets

AutoAir stores no secrets. Credentials reach the pods as environment variables from Kubernetes
Secrets that these manifests reference but never contain; git, LLM and validator credentials
are Airflow connections (`AIRFLOW_CONN_<ID>`), the same format Airflow reads.

| Secret | Key | Variable | Used by |
|---|---|---|---|
| `autoair-db` | `password` | `AUTOAIR_DATABASE__PASSWORD` | postgresql (also AI and MCP pods if present) |
| `autoair-collab` | `secret` | `AUTOAIR_COLLAB_SECRET` | production (several replicas) |
| `autoair-git` | `connection` | `AIRFLOW_CONN_GIT_DAGS` (`storage.git.conn_id=git_dags`) | git-target |
| `autoair-validator` | `connection` | `AIRFLOW_CONN_AUTOAIR_VALIDATOR` | validator (both sides) |
| `autoair-ai` | `shared-secret`, `llm-connection`, optional `fernet-key` | `AUTOAIR_AI__SERVICE__SHARED_SECRET`, `AIRFLOW_CONN_AUTOAIR_LLM`, `AUTOAIR_AI__USER_LLM__ENCRYPTION_KEY` | ai-service |
| `autoair-tls` | `tls.crt`, `tls.key` | | ingress (not used with gateway-api) |

```bash
ns=autoair
kubectl -n $ns create secret generic autoair-db --from-literal=password='<database password>'
kubectl -n $ns create secret generic autoair-collab --from-literal=secret="$(openssl rand -hex 32)"
kubectl -n $ns create secret generic autoair-git --from-literal=connection='{"conn_type": "git",
  "host": "https://github.com/acme/airflow-dags.git", "login": "x-access-token",
  "password": "<fine-grained token>"}'
kubectl -n $ns create secret generic autoair-validator --from-literal=connection=\
"{\"conn_type\": \"generic\", \"password\": \"$(openssl rand -hex 32)\"}"
```

With External Secrets Operator, Sealed Secrets or the Secrets Store CSI driver, produce Secrets
with the same names and keys; nothing else changes. An Airflow secrets backend (Vault, AWS
Secrets Manager, ...) configured with `AIRFLOW__SECRETS__BACKEND` on the pods can serve the
connections instead of `AIRFLOW_CONN_*` variables.

## Image

`base/kustomization.yaml` and every overlay pin `ghcr.io/ctolon/autoair` with `images:` (the
overlays repeat the entry so it reaches the containers that components add; `validate.sh`
fails when the tags differ). Bump all of them with every release. Your own image with the
providers your DAGs import (`deploy/docker/Dockerfile`) goes in your overlay:

```yaml
images:
  - name: ghcr.io/ctolon/autoair
    newName: registry.example.com/my-airflow-autoair
    newTag: "3.3.2-1"
```

## Hardening

Every pod runs as uid 50000 (the image's `airflow` user), with `runAsNonRoot`, a read-only
root file system, no capabilities, no privilege escalation, the `RuntimeDefault` seccomp
profile and no service account token; the namespaces of the overlays enforce the `restricted`
Pod Security Standard. Writable paths are `emptyDir` volumes with size limits (`/tmp`, which is
also `HOME`) and the DAG volume. `AIRFLOW_HOME` stays `/opt/airflow`, the folder the image's
precomputed operator catalog was built for.

## Validate

```bash
deploy/kustomize/validate.sh      # needs kustomize and kubeconform, and network for schemas
```
