---
paths:
  - "src/autoair/ai/**"
  - "src/autoair/api/routers/ai*.py"
  - "frontend/src/ai/**"
  - "tests/**/test_ai_*.py"
---

# AI features

- `pydantic_ai` (extra `autoair[ai]`) is imported only by modules loaded when AI is used.
  `api/routers/ai_mount.py` decides at start-up whether the in-process AI routes are mounted,
  without importing pydantic-ai. Keep `pydantic_ai` imports out of every module the core
  install loads; `uv run nox -s noai` proves it (fresh venv with `.[standalone,mcp]`, runs
  `tests/integration/test_ai_unavailable.py` and `test_kb_noai.py`).
- Settings live in `ai/config.py` (`AiSettings`), which must import without `autoair[ai]`.
- pydantic-ai providers get an `httpx2.AsyncClient` from `net/tls.py`; only the Groq and Cohere
  SDKs bring their own `httpx` clients (`ai/models.py::LEGACY_HTTPX`), so `outbound.*` does not
  apply to them.
- Guardrails on input and output (`ai/guardrails.py`: secret redaction, PII masking, injection
  shield, budgets, tool allowlist); tool results are untrusted. Agent file access goes only
  through `ai/assistants/fs_sandbox.py`. Proposed changes are diffs applied through the normal
  deploy pipeline.
- The separate AI service (`autoair ai-serve`) trusts an HMAC-signed identity
  (`ai.service.shared_secret`, replay window in `ai/access.py`) or verifies Airflow tokens.
- Troubleshoot Airflow actions (`ai.troubleshoot.airflow_actions` = `read_only` / `hitl` /
  `apply`, `assistants/airflow_actions.py`) run with the caller's token and `run:manage`. A
  `hitl` execution is claimed first (`status=running`, compare-and-set) before Airflow is
  called; test races with two threads on SQLite (`tests/unit/test_troubleshoot_action_claim.py`).
- Action results carry a stable `code` + `params` next to the English message; the UI
  translates them as `ai:troubleshoot.result.<code>` (`resultText()` in
  `frontend/src/ai/AirflowActions.tsx`). A new code needs en and tr strings.
- Long AI responses stream; do not buffer whole responses in memory.
- Per-user LLM keys (`ai/user_llm.py`, opt-in, Fernet-encrypted) are the one approved stored
  secret; do not add others.
- Fake LLM for AI tests: `uv run python scripts/e2e_fake_llm.py --port <port>`.
