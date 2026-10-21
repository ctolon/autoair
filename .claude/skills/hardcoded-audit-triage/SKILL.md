---
name: hardcoded-audit-triage
description: Triage findings of the hard-coded value audit (scripts/scan_hardcoded.py, decisions in scripts/hardcoded_decisions.json) - decide per literal between a setting and keeping it in code. Use when `scan_hardcoded.py --check` fails, or after adding timeouts, sizes, URLs, paths, regexes or module constants under src/autoair.
---

# Triage the hard-coded value audit

`scripts/scan_hardcoded.py` walks the AST of `src/autoair` and reports module constants,
numeric defaults and kwargs, numbers >= 100, regexes, URLs and paths. Each finding has a
stable key `file::scope::kind::name` (no line number). `scripts/hardcoded_decisions.json` is
the committed source of truth: `rules` (regex on the key, for whole classes such as HTTP
status codes) and `decisions` (per key; explicit decisions win).

Paths audited by their owners are excluded in the script (`EXCLUDED`: `ai/`, `mcp/`, `iam/`,
`auth.py`, `api/routers/ai.py`, the scaffold template).

## Commands

```bash
uv run python scripts/scan_hardcoded.py            # untriaged findings
uv run python scripts/scan_hardcoded.py --json     # every finding
uv run python scripts/scan_hardcoded.py --check    # CI: all triaged, none stale
uv run python scripts/scan_hardcoded.py --write    # report: .artifacts/hardcoded-audit.md (gitignored)
```

## Deciding

| Make it `config` when | `keep` when |
|---|---|
| an operator could reasonably need another value: timeouts, limits, sizes, retention, URLs of external services, paths | protocol or format constants (HTTP status, header names, schema bounds), internal mechanics with no user-visible effect, compatibility data, security floors that must not be lowered |

- `config`: add the setting first (skill add-setting), then
  `"<key>": {"decision": "config", "setting": "<section.field>"}`. A constant that stays as
  the fallback for callers without settings says so in `setting`.
- `keep`: `"<key>": {"decision": "keep", "reason": "<why it must not change>"}`. The reason
  is for a reviewer: name the protocol, invariant or risk, not "not needed".
- Stale decisions (the value was removed or renamed): delete the entry; when renamed, move
  the decision to the new key instead of re-deciding.
- A security-relevant value (token lengths, masks, size caps) is never made lower than its
  current default by configuration; say so in the reason when kept.

## Pitfalls

- Keys include the scope: moving a constant into a function or class changes its key.
- Do not add a broad `rules` pattern to silence one finding; rules are for whole classes.
- The markdown report is local only; never commit `.artifacts/`.

## Done when

- [ ] `scan_hardcoded.py --check` passes
- [ ] Every new `config` decision names an existing setting; `gen_config_docs.py --check` passes
