---
paths:
  - "src/autoair/api/**"
  - "src/autoair/iam/**"
  - "src/autoair/auth.py"
  - "tests/integration/test_api_*.py"
---

# API, IAM and feature gates

Procedure for a new or changed endpoint: skill `add-api-route`.

- AutoAir has no user database. Airflow authenticates; AutoAir maps identities to roles
  (strategies `airflow_permissions`, `role_mapping`, `custom_view`, `iam/resolver.py`) and checks
  fine-grained permissions (`builder:use`, `spec:write`, `deploy`, `deploy:direct_push`,
  `code:execute`, `code:raw`, `ai:use`, `admin:config`, ...; constants in `iam/model.py`).
  Airflow calls forward the caller's token (`GatewayDep`), so Airflow RBAC applies on top; never
  a service identity.
- Gates are attached in `create_api` (`api/app.py`) or on the route, never inside the handler:
  `require(P.<PERMISSION>)` from `autoair.iam.deps`, and `feature("<name>")` (404 while the flag
  is off, evaluated per request). Inside a handler use `ensure_feature(request, name)`. Read and
  write endpoints of one area sit on separate routers because they need different permissions.
  A new permission is an owner decision (it changes `BUILTIN_ROLES` and `docs/iam.md`).
- Four-eyes approval (`iam.require_approval`) routes deploys through proposals
  (`iam/proposals.py`); `deploy:direct_push` bypasses it and is privileged.
- Cookie-authenticated (SSO) state-changing requests require the `X-AutoAir-CSRF` header; no
  `GET` with side effects. Collaboration WebSockets use short-lived tickets that require DAG
  edit permission.
- Bound everything the client controls (`Query(max_length=...)`, page and upload sizes from
  the `limits` section); expensive endpoints join a `limits.rate_limits` group. Unbounded list
  endpoints are a review blocker.
- Errors: `AirflowError` through `http_error(...)`; `DocumentConflict` maps to 409
  (`install_document_errors`); git-token code runs inside `api/git_token.scrubbed_errors`.
  `GET /api/config` masks `SecretStr` values.
- Tests cover allowed, forbidden (403) and feature-off (404) for every new route
  (`tests/integration/test_api_security.py`, `test_airflow_feature_gate.py`). Then
  `cd frontend && npm run codegen`.
