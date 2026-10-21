---
title: Kubernetes with Kustomize
description: Deploy the standalone AutoAir server and its optional validator, AI and MCP services to Kubernetes from the Kustomize base, components and overlays in deploy/kustomize.
sidebar_label: Kubernetes with Kustomize
---

[`deploy/kustomize`](https://github.com/ctolon/autoair/tree/main/deploy/kustomize) holds raw Kubernetes manifests for the standalone server (`autoair serve`) and its optional services, assembled with Kustomize: a base, Kustomize Components for the optional pieces, and overlays that combine them. They build with `kubectl kustomize` (`kubectl apply -k`) or the `kustomize` binary.

Use them when AutoAir runs next to Airflow as its own service on its own host name. For AutoAir as a plugin inside an Airflow installed with the official Helm chart, use [Plugin with the Airflow Helm chart](/docs/deployment/helm-plugin) instead; the [Kubernetes reference architecture](/docs/deployment/kubernetes) explains how the pieces fit together.

## Layout

| Path | Contents |
|---|---|
| `base/` | `autoair serve`: Deployment, Service, ServiceAccount, PodDisruptionBudget, ConfigMap `autoair-config` from `settings.env`. One replica, file state, local deploy target on an `emptyDir` |
| `components/postgresql` | AutoAir state in PostgreSQL, `autoair db upgrade` as an init container, `migrate_on_start=false` |
| `components/git-target` | Deploy target git: specs and generated DAGs go to a repository by pull request |
| `components/validator` | `autoair validator-serve` with a NetworkPolicy that allows no egress; the server's import checks go to it with `fail_mode=open` (the default; set `closed` in `client.env` to block deploys while it is down) |
| `components/ai-service` | `autoair ai-serve`; the server proxies `/api/ai/*` and `/mcp` to it |
| `components/mcp-http` | `autoair mcp-serve --http` on port 8766 (optional: the server and the AI service already serve `/mcp`) |
| `components/ingress` | Ingress with timeouts and unbuffered responses for AI streams, plus the public URL settings; use it instead of `gateway-api` with an Ingress controller |
| `components/gateway-api` | An `HTTPRoute` attached to an existing Gateway (TLS terminates there), plus the public URL settings; the production default |
| `components/hpa` | HorizontalPodAutoscaler, 2 to 6 replicas on CPU and memory |
| `components/network-policies` | NetworkPolicies for the server, the AI service and the MCP server |
| `components/service-monitor` | Prometheus Operator `ServiceMonitor`s for `/api/metrics` and the validator's `/metrics` |
| `components/airflow-plugin` | Plugin mode for an Airflow deployed from plain manifests (see [below](#plugin-mode-with-kustomize)) |
| `overlays/dev` | One replica, file state and DAGs on a 1 GiB PersistentVolumeClaim, no Secrets |
| `overlays/production` | PostgreSQL, git, validator, Gateway API `HTTPRoute`, HPA, PDB with `minAvailable: 1`, NetworkPolicies |
| `overlays/airflow-plugin` | The `airflow-plugin` component applied to a stand-in api-server Deployment |

## Development overlay

```bash
# set AUTOAIR_AIRFLOW_URL in deploy/kustomize/overlays/dev/settings.env first
kubectl apply -k deploy/kustomize/overlays/dev
kubectl -n autoair-dev rollout status deploy/autoair
kubectl -n autoair-dev port-forward svc/autoair 8765:8765
```

The UI is then at `http://localhost:8765/ui/`. Users sign in with their Airflow credentials, which AutoAir verifies against `AUTOAIR_AIRFLOW_URL`. DAGs go to the PersistentVolumeClaim (`storage.mode=local`); point your DAG processor at the same files, or use the git target for anything beyond a trial. The Deployment uses the `Recreate` strategy because a `ReadWriteOnce` volume cannot be mounted by two pods.

## Production overlay

1. Copy `overlays/production` into your configuration repository. Keep the relative paths to `base` and `components`, or reference this repository as a remote base with a `?ref=` tag.
2. Edit `overlays/production/settings/settings.env`: Airflow's internal and public URLs, `AUTOAIR_SERVER__PUBLIC_URL`, `AUTOAIR_SERVER__FORWARDED_ALLOW_IPS` (the Gateway's or ingress controller's pod network), the PostgreSQL URL and the git repository.
3. Point the `HTTPRoute` at your Gateway (`parentRefs`, `hostnames` in `components/gateway-api/httproute.yaml`), or swap in `components/ingress` and patch its host and TLS Secret; then adjust the namespace and pod selectors in the NetworkPolicies (the Gateway or ingress controller, Airflow, DNS). The Gateway API is the default because it is the Kubernetes successor to Ingress and needs no controller-specific annotations.
4. Create the Secrets listed below.
5. Apply:

```bash
kubectl apply -k deploy/kustomize/overlays/production
kubectl -n autoair rollout status deploy/autoair
```

`settings/` is a Component listed last in the overlay. Components merge their keys into the ConfigMap `autoair-config` in the order they are listed, and a `configMapGenerator` in the overlay itself runs before the components; a setting in the last Component therefore wins over every component's default.

The validator component runs `autoair validator-serve`, which exists from AutoAir 0.2.0. With an older image, remove `../../components/validator` from the overlay.

The AI service and the MCP server are commented out in the overlay. They read the same ConfigMap as the server and share no volume with it, so use them together with `components/postgresql` and `components/git-target`.

## Secrets

AutoAir stores no secrets. The manifests reference Kubernetes Secrets by name and never contain values. Git, LLM and validator credentials are Airflow connections, passed as `AIRFLOW_CONN_<ID>` environment variables in Airflow's JSON form:

| Secret | Keys | Becomes | Needed by |
|---|---|---|---|
| `autoair-db` | `password` | `AUTOAIR_DATABASE__PASSWORD` | `postgresql` (also read by the AI and MCP pods when present) |
| `autoair-collab` | `secret` | `AUTOAIR_COLLAB_SECRET` | `overlays/production`: collaboration tickets must verify on every replica |
| `autoair-git` | `connection` | `AIRFLOW_CONN_GIT_DAGS` (`AUTOAIR_STORAGE__GIT__CONN_ID=git_dags`) | `git-target` |
| `autoair-validator` | `connection` | `AIRFLOW_CONN_AUTOAIR_VALIDATOR`, the HMAC key in `password` (16 bytes or more) | `validator`, on the validator and on the server |
| `autoair-ai` | `shared-secret`, `llm-connection`, optional `fernet-key` | `AUTOAIR_AI__SERVICE__SHARED_SECRET`, `AIRFLOW_CONN_AUTOAIR_LLM` (`AUTOAIR_AI__LLM_CONN_ID=autoair_llm`), `AUTOAIR_AI__USER_LLM__ENCRYPTION_KEY` | `ai-service` |
| `autoair-tls` | `tls.crt`, `tls.key` | | `ingress` (not needed with `gateway-api`) |

```bash
ns=autoair
kubectl -n $ns create secret generic autoair-db --from-literal=password='<database password>'
kubectl -n $ns create secret generic autoair-collab --from-literal=secret="$(openssl rand -hex 32)"
kubectl -n $ns create secret generic autoair-git --from-literal=connection='{"conn_type": "git",
  "host": "https://github.com/acme/airflow-dags.git", "login": "x-access-token",
  "password": "<fine-grained token>"}'
kubectl -n $ns create secret generic autoair-validator --from-literal=connection=\
"{\"conn_type\": \"generic\", \"password\": \"$(openssl rand -hex 32)\"}"
kubectl -n $ns create secret generic autoair-ai \
  --from-literal=shared-secret="$(openssl rand -hex 32)" \
  --from-literal=llm-connection='{"conn_type": "pydanticai", "password": "<API key>",
    "extra": {"model": "anthropic:claude-sonnet-5-5"}}'
```

External Secrets Operator, Sealed Secrets or the Secrets Store CSI driver work unchanged when they produce Secrets with these names and keys. An Airflow secrets backend configured on the pods with `AIRFLOW__SECRETS__BACKEND` can serve the connections instead of the `AIRFLOW_CONN_*` variables.

## Image

The base and every overlay pin `ghcr.io/ctolon/autoair` with an `images:` entry. The overlays repeat the entry because an overlay's `images:` also reaches the containers its components add (the migration init container, the validator, the AI and MCP pods); `deploy/kustomize/validate.sh` fails when the tags differ. Replace it with your own image when your DAGs import providers the release image lacks ([Building the image](/docs/deployment/helm-plugin#building-the-image)):

```yaml
images:
  - name: ghcr.io/ctolon/autoair
    newName: registry.example.com/my-airflow-autoair
    newTag: "3.3.2-1"
```

## Probes and shutdown

| Pod | Startup | Readiness | Liveness |
|---|---|---|---|
| `autoair serve` | `GET /api/health`, up to 3 minutes | `GET /api/ready` (catalog, deploy target, and Airflow with `AUTOAIR_SERVER__READY_CHECK_AIRFLOW=true` in production) | `GET /api/health` |
| `autoair ai-serve` | `GET /health` | `GET /ready` | `GET /health` |
| `autoair validator-serve` | `GET /ready` | `GET /ready` | `GET /health` |
| `autoair mcp-serve --http` | TCP port 8766 | TCP port 8766 | TCP port 8766 |

`/api/ready` answers 503 while the deploy target is unreachable, which takes a replica out of rotation without restarting it; see [Probes](/docs/deployment/kubernetes#probes). On shutdown the server waits 5 seconds in `preStop` so the ingress stops sending requests, then gives in-flight requests `AUTOAIR_SERVER__GRACEFUL_SHUTDOWN_TIMEOUT` (25 s) within a 40-second grace period.

## Security context

Every pod runs as uid 50000 (the image's `airflow` user) with `runAsNonRoot`, a read-only root file system, all capabilities dropped, no privilege escalation, the `RuntimeDefault` seccomp profile and no service account token. The overlays' namespaces enforce the `restricted` Pod Security Standard. Writable paths are size-limited `emptyDir` volumes for `/tmp` (also `HOME`, for git) and the DAG volume. `AIRFLOW_HOME` stays `/opt/airflow` on the server, the AI and the MCP pods: the image's precomputed operator catalog (`AUTOAIR_CATALOG_FILE`) was built for it and is ignored as stale under another `AIRFLOW_HOME`.

## Network policies

`components/network-policies` lets into the server only the Gateway's namespace (`gateway-system` in the example; your ingress controller's with `components/ingress`), and lets out DNS, Airflow's api-server (pods labelled `component: api-server` in the namespace `airflow`), the validator, the AI service, and TCP 5432 and 443 to any address except the cloud metadata endpoint. The AI and MCP policies follow the same pattern. Narrow the 5432/443 rule to your database and git host; whether an `ipBlock` matches pod addresses inside the cluster depends on the network plugin, so add a `podSelector` rule for an in-cluster PostgreSQL. With `components/service-monitor`, the policy `autoair-metrics` lets the `monitoring` namespace scrape ports 8765 and 8791.

The validator's own policy allows ingress on 8791 from the AutoAir pods and from Airflow api-server pods (`component: api-server`) in the namespace `airflow`, and no egress at all, DNS included. The namespace is a placeholder like the one in `components/network-policies`: set it to the namespace Airflow runs in.

## Plugin mode with Kustomize

`components/airflow-plugin` turns an Airflow that you deploy from plain manifests into one with the AutoAir plugin:

- an `images:` entry replaces `apache/airflow` with `ghcr.io/ctolon/autoair:<version>-airflow3.3.2` in every Airflow component, so the scheduler, DAG processor and workers import the same providers as the operator palette;
- a patch adds the AutoAir settings to every Deployment labelled `component=api-server` (the official chart's label), in its container `api-server`: git deploy target from the `GitDagBundle`, PostgreSQL state, `AUTOAIR_DATABASE__PASSWORD` from the Secret `autoair-db`.

`overlays/airflow-plugin` shows it on a stand-in Deployment; replace that file with your Airflow manifests. When Airflow is installed with the official Helm chart, use [`deploy/helm/values-autoair.yaml`](https://github.com/ctolon/autoair/blob/main/deploy/helm/values-autoair.yaml): the chart already exposes the image, environment and Secret references, and a `helm upgrade` would replace manifests patched outside Helm.

## Validate

```bash
deploy/kustomize/validate.sh
```

The script builds every overlay, the base, and one combination with every component, and validates the output with `kubeconform -strict` against the Kubernetes 1.31 schemas and, for `ServiceMonitor` and `HTTPRoute`, schemas from the CRDs catalog. It needs `kustomize` and `kubeconform` (or `KUSTOMIZE` / `KUBECONFORM` pointing at them) and network access for the schemas. CI runs it on every change.

## Related pages

- [Kubernetes reference architecture](/docs/deployment/kubernetes)
- [PostgreSQL for AutoAir state](/docs/deployment/postgres)
- [Isolated import checks (validator service)](/docs/deployment/validator-service)
- [Separate AI service](/docs/deployment/ai-service)
- [Standalone behind a proxy](/docs/deployment/standalone-proxy)
