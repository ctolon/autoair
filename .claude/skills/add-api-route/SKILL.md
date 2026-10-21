---
name: add-api-route
description: Add or change an AutoAir HTTP API endpoint (FastAPI router under src/autoair/api/routers). Use when adding a route, changing a request/response model, or changing who may call an endpoint; covers IAM permissions, feature gates, errors and regenerating the TypeScript client.
---

# Add an API route

The API is mounted at `/api` (standalone) and `/autoair/api` (plugin). `create_api` in
`src/autoair/api/app.py` wires every router; IAM and feature gates are attached there or on
the route, never inside the handler body.

## Steps

1. Pick the router in `src/autoair/api/routers/` by area, or add a module. Read and write
   endpoints of one area live on separate routers (`read_router` / `write_router`) because
   they need different permissions.
2. Request and response bodies are Pydantic models with field descriptions; they become the
   OpenAPI schema and the generated client types. Return a model, not a bare `dict`.
3. Dependencies, from `src/autoair/api/deps.py`: `ServicesDep` (services), `GatewayDep`
   (Airflow client that forwards the caller's own token, so Airflow RBAC applies).
4. Gate the router in `create_api` (or a single route with `dependencies=[...]`):
   - permission: `require(P.SOME_PERMISSION)` from `autoair.iam.deps`, with `P` =
     `autoair.iam.model`. Pick the narrowest existing permission; a new permission is an
     owner decision (it changes roles in `BUILTIN_ROLES` and `docs/iam.md`);
   - feature switch: `feature("name")` (alias of `autoair.api.features.requires_feature`),
     which answers 404 while `features.<name>` is off. It is evaluated per request, so admin
     overrides (`feature_overrides.py`) apply without a restart. Inside a handler use
     `ensure_feature(request, name)`.
   - DAG-scoped writes reuse `auth.write_dag` like the specs router.
5. Errors: raise `HTTPException(status_code, detail=...)` with an actionable message;
   `AirflowError` goes through `http_error(...)`; `DocumentConflict` maps to 409 by
   `install_document_errors`. Never put a token, header value or connection password in
   `detail`; wrap git-token code in `api/git_token.scrubbed_errors`.
6. Bound everything the client controls: `Query(max_length=...)`, page sizes, upload sizes
   (`limits` section). Unbounded list endpoints are a review blocker.
7. Outbound HTTP from the handler uses `autoair.net.tls.async_client(...)`.
8. Tests: an integration test in `tests/integration/` covering the happy path, the 403
   without the permission, and the 404 with the feature off (see `test_api_security.py`,
   `test_airflow_feature_gate.py`).
9. Regenerate the client and commit both outputs:

   ```bash
   cd frontend && npm run codegen     # exports openapi.json, regenerates src/api/gen
   npm run typecheck
   ```

## Pitfalls

- Mutable process-local state (dicts, caches that must agree across replicas) breaks
  multi-replica deployments; store it through `persistence.document_store(...)`.
- The plugin and standalone modes mount the same API under different prefixes; build URLs
  in the frontend through the generated client, never by string concatenation.
- Rate-limited groups (`limits.rate_limits`): expensive endpoints join an existing group.

## Done when

- [ ] `require(...)` and, if user-facing, `feature(...)` applied
- [ ] Tests for allowed, forbidden and feature-off
- [ ] `npm run codegen` run; `frontend/openapi.json` and `frontend/src/api/gen` updated
- [ ] `uv run ruff check src tests && uv run mypy` clean
