---
paths:
  - "CLAUDE.md"
  - ".claude/**"
---

# Maintaining the Claude Code setup

- `CLAUDE.md` holds only what applies to every task and stays under 200 lines; area detail
  goes into a rule here with `paths:` frontmatter (the only field rules read), procedures into a
  skill. State a fact in one place.
- Skills: `.claude/skills/<name>/SKILL.md`, folder name = `name` (lowercase, hyphens);
  `description` says what it does and when to use it, naming the files, commands and phrases
  that should trigger it (description + `when_to_use` are cut at 1,536 characters). Body: exact
  commands, repo pitfalls, a "Done when" checklist; reference bundled scripts as
  `${CLAUDE_SKILL_DIR}/scripts/...`. Helper scripts never touch shared resources.
  `disable-model-invocation: true` (as in `release`) also keeps a skill out of subagents'
  `skills:` preload.
- Subagents: `.claude/agents/<name>.md` with `name`, `description` (when to delegate), the
  smallest `tools` set (reviewers have no Edit/Write), `model`, optional `skills` to preload.
  They load CLAUDE.md and rules themselves; do not paste rules into their prompts.
- Hooks: Python 3 standard library, one JSON object on stdin, decisions through the documented
  JSON output (`hookSpecificOutput.permissionDecision` for PreToolUse,
  `hookSpecificOutput.additionalContext` for Stop/SubagentStop). Every script fails open.
  Commands in `settings.json` use `python3 "$CLAUDE_PROJECT_DIR"/.claude/hooks/<script>.py`.
  Running sessions pick up `settings.json` edits through the file watcher; a hook command that
  names a missing script makes every matching tool call fail, so never move a script before
  `settings.json` points at its new path.
- A new generated file: extend `GENERATED` in `.claude/hooks/_lib.py` and the "Generated files"
  table in `CLAUDE.md`, then add a case to `test_hooks.py`.
- Check: `python3 .claude/hooks/test_hooks.py`, `jq . .claude/settings.json`.
