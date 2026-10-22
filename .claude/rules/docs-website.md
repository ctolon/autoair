---
paths:
  - "docs/**"
  - "website/**"
  - "README.md"
  - "CONTRIBUTING.md"
---

# Documentation and website

Procedure: skill `docs-page`; larger pages: subagent `docs-writer`.

- Two places, one job each. `docs/*.md` is the in-repo reference read on GitHub: the generated
  configuration reference, configuration mechanics, architecture (`docs/architecture/`), the
  IAM/SSO security model, MCP and AI internals, live test suites.
  `website/docs/<section>/` is the Docusaurus 3.10 user site (Mermaid, local search):
  installation, user guide, administration how-to (including the policy reference: switches,
  secret detection, `policy.rules`, OPA in `website/docs/admin/policy.md`), AI, extending,
  roadmap. A topic is written in one place; the other links to it instead of repeating it
  (`docs/policy.md` is only a pointer to the policy page).
- Cross-links: a `docs/` page opens with one line naming its website guide, as an absolute
  URL (`https://ctolon.github.io/autoair/docs/<section>/<page>`; origin and base path from
  `website/site.config.ts`). Website pages link to repository files with GitHub URLs
  (`https://github.com/ctolon/autoair/blob/main/docs/...`); only the synced configuration
  reference is a site route. Do not rename or delete a `docs/` file without updating `LINKS` in
  `website/scripts/sync-reference.mjs` and the code comments and descriptions that cite it.
- Site layout: sidebar `website/sidebars.ts` (every page listed), settings `site.config.ts`,
  scripts in `website/scripts/` (`sync-reference.mjs` copies the configuration reference before
  `start`/`build`; `export-api.mjs`, `check-mdx.mjs`, `check-site.mjs`, `llms-txt.mjs`,
  `capture/`). Contributor guide: `website/docs/contributing/documentation.mdx`.
- Page front matter: `title`, a one-sentence `description` (it becomes the page's line in
  `llms.txt`), optional `sidebar_label`; no `id`. `.mdx` only for `<Screenshot>` / `<Video>`;
  in `.mdx`, `{`, `}` and `<` in prose are JSX, so use code spans.
- Links: site routes without extension (`/docs/admin/iam#heading`). Repository docs linked from
  synced pages need a route in `LINKS` in `website/scripts/sync-reference.mjs`.
- Every setting, UI label, endpoint and command is checked against the code before it is
  written: settings in `docs/configuration-reference.md`, labels in
  `frontend/src/i18n/locales/en/*.json`, endpoints in `src/autoair/api/routers/`, CLI options
  from `uv run autoair <command> --help`. Do not document what you cannot find.
- Style: precise, plain English; lead with the task and the exact command or setting; tables
  for options; one example per concept; defaults and limits stated; unsupported cases named. No
  filler, marketing adjectives or emoji.
- Check: `cd website && node scripts/check-mdx.mjs <files>`, `npm run typecheck`,
  `npm run build` (broken links fail), `npm run check:site -- /docs/<section>/<page>` (serves on
  `CHECK_SITE_PORT`, default 8868; never the shared :3000).
