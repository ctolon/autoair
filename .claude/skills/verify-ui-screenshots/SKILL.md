---
name: verify-ui-screenshots
description: Take and review screenshots of an AutoAir UI change in light and dark mode, in plugin (inside Airflow) and standalone mode, and fail on page or console errors. Use for any visible frontend change before reporting it done or attaching it to a PR.
allowed-tools: Bash(node ${CLAUDE_SKILL_DIR}/scripts/shoot.mjs *)
---

# Verify a UI change with screenshots

The owner's rule: every visible change is checked in **light and dark** and in **plugin and
standalone** mode, and invalid input never breaks a panel. `scripts/shoot.mjs` (next to this
file) opens the UI read-only in each combination, saves PNGs and exits 1 on a `pageerror` or a
console error.

## Standalone (private, preferred)

```bash
eval "$("${CLAUDE_PROJECT_DIR}"/.claude/skills/run-e2e/scripts/private-stack.sh start)"     # private API + vite preview
node ${CLAUDE_SKILL_DIR}/scripts/shoot.mjs \
  --modes standalone --standalone-url "$AUTOAIR_DEV_URL/" --airflow-url http://127.0.0.1:9
"${CLAUDE_PROJECT_DIR}"/.claude/skills/run-e2e/scripts/private-stack.sh stop
```

The private stack builds the current tree into its own folder and runs `autoair serve
--no-auth` with its own state, so nothing shared changes.

## Plugin (inside Airflow)

The plugin bundle is served from `src/autoair/static/plugin/` by whichever Airflow runs the
editable install. To see your change in the shared Airflow (:8080) you must rebuild that
bundle, which changes the demo for everyone: do it only from a tree that type-checks.

```bash
(cd frontend && npm run build:plugin)
node ${CLAUDE_SKILL_DIR}/scripts/shoot.mjs --modes plugin --dag <dag_id>
```

Inside Airflow the theme follows the host (`HOST_THEMED` in `frontend/src/shell/colorMode.ts`);
the script sets both the browser colour scheme and Airflow's `theme` storage key.

## Options

`--dag <id>` opens a DAG; `--click "<text>"` clicks an element first (open a dialog or tab);
`--lang tr` checks Turkish; `--viewport 1280x800`; `--out <dir>` (default
`.artifacts/screenshots/<timestamp>/`, gitignored).

## Review

Open every PNG (the Read tool shows images). Check: contrast and borders in dark mode
(semantic tokens, no hard-coded colours), truncation in Turkish, no overlapping panels,
focus states, empty and error states. Then feed the panel invalid input (wrong types, empty
required fields, a Jinja string in a number field) and shoot again: the panel must show a
problem, not an error boundary.

## Done when

- [ ] 4 screenshots (plugin/standalone x light/dark), or the reason a mode does not apply
- [ ] script exit code 0 (no page or console errors)
- [ ] invalid-input state checked
- [ ] screenshots attached to the PR description when there is one
