---
name: docs-page
description: Write or update AutoAir documentation - reference docs in docs/ or a Docusaurus page in website/docs - with every claim verified against the code. Use for new pages, documenting a feature, setting, endpoint or CLI command, or fixing docs drift.
---

# Write a docs page

Two places, different jobs; a topic lives in one of them and the other links to it (policy and
link conventions: `.claude/rules/docs-website.md`):

| Path | What | Built by |
|---|---|---|
| `docs/*.md` | In-repo reference read on GitHub: configuration mechanics and the generated reference, architecture, IAM/SSO security model, policy rules, MCP and AI internals | nothing (plain Markdown) |
| `website/docs/**` | The user site (Docusaurus 3.10): installation, user guide, administration, AI, extending | `cd website && npm run build` |

Generated, never hand-edited: `docs/configuration-reference.md` (from `Settings` via
`scripts/gen_config_docs.py`), `website/docs/reference/configuration.md` (copied by
`npm run sync`), `website/static/openapi.json` and `website/static/schemas/dag-spec.schema.json`
(`npm run export-api`), `website/build/llms*.txt`. The full contributor guide is
`website/docs/contributing/documentation.mdx`.

## Steps

1. Find the facts in the code first: setting names and env vars in the configuration
   reference, UI labels in `frontend/src/i18n/locales/en/*.json`, API paths in
   `src/autoair/api/routers/`, CLI options from `uv run autoair <cmd> --help`. Do not
   document anything you cannot find.
2. Website page: `website/docs/<section>/<page>.md` (`.mdx` only for `<Screenshot>` /
   `<Video>`); front matter `title`, one-sentence `description` (it becomes the page's line
   in `llms.txt`), optional `sidebar_label`; no `id`. Add the page to `website/sidebars.ts`.
3. Links: site routes without extension (`/docs/admin/iam#heading`). Repository docs linked
   from synced pages need a route in `LINKS` in `website/scripts/sync-reference.mjs`.
4. In `.mdx`, `{`, `}` and `<` in prose are JSX: use code spans.
5. Check:

   ```bash
   cd website
   node scripts/check-mdx.mjs docs/<section>/<page>.md   # fast, no build
   npm run typecheck
   npm run build                                          # broken links/anchors fail
   npm run check:site -- /docs/<section>/<page>           # light/dark, desktop/phone
   ```

   `check:site` serves the build on `CHECK_SITE_PORT` (default 8868); the shared docs
   server on :3000 is not touched. Screenshots land in `.artifacts/check-site/`.
6. User-visible change: CHANGELOG entry under `[Unreleased]`.

## Style

Precise, plain English. Lead with what the reader can do and the exact command or setting.
No filler, no marketing adjectives, no emoji, no "simply"/"just"/"powerful"/"seamless".
Tables for options; one example per concept; state defaults and limits. Say what is not
supported. Use the delegate `docs-writer` subagent for larger pages.

## Done when

- [ ] Every setting, label, path and command checked against the code
- [ ] Page listed in `sidebars.ts`; `npm run build` passes; `check:site` clean for the page
- [ ] No generated file edited by hand
