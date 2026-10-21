---
paths:
  - "src/autoair/storage/**"
  - "src/autoair/secret_mask.py"
  - "src/autoair/net/**"
  - "src/autoair/ai/net.py"
  - "src/autoair/gateway/**"
  - "src/autoair/governance.py"
  - "src/autoair/marketplace.py"
---

# Storage, git, secrets and outbound network

New or changed git host: skill `add-git-provider`. Security review of these files: skill
`security-review`.

- Deploy targets implement `DeployTarget` (`storage/base.py`); git providers implement the
  `GitProvider` protocol (`storage/git/provider.py`). Git operations run through the git CLI
  (`local_git.py`) with arguments validated (`check_branch` for user branch names) and passed as
  argv, never through a shell.
- Git credentials come from an Airflow connection (`storage/git/config.py::credentials_from_connection`)
  or the per-request token (`storage/request_token.py`). The token is accepted only over HTTPS
  or from a loopback client, sent only to `https://` hosts, never cached, pickled, persisted,
  returned or put in a URL that can be logged; error paths run under `secret_mask.masking`.
- `secret_mask.py` masks registered secrets in logs, audit records and errors
  (`install_log_masking`); code that handles a secret runs inside
  `secret_mask.masking(secret)`.
- `net/tls.py` (`async_client`, `httpx_kwargs`, `transport_kwargs`) applies the CA bundle,
  client certificates and proxies per client (`outbound.clients.<name>`). Bound timeouts and
  response sizes on every call.
- User-controlled URLs (Airflow, git, marketplace, AI, MCP, research, KB) resolve once and
  connect to the checked public IP (`ai/net.py`: `check_url`, `PinnedAsyncTransport`); private
  ranges only when explicitly allowed (`allow_private`). A client that follows redirects
  re-checks each hop.
- `gateway/` forwards the caller's credentials to Airflow's REST API. Endpoints added after
  Airflow 3.0 are called and a 404/405 means "not available"; the result is exposed through
  `/api/airflow/capabilities` (`api/routers/airflow.py`, `AirflowCapabilities`).
- Marketplace indexes are Ed25519-signed; `require_signature` defaults to true.
