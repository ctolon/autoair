# AutoAir documentation website

The documentation site for AutoAir, built with [Docusaurus 3](https://docusaurus.io/) and published
to GitHub Pages by `.github/workflows/pages.yml` (pushes to `main`).

## Commands

Node.js 20 or later (CI uses 22).

```bash
npm ci                 # once
npm start              # dev server with live reload: http://localhost:3000/autoair/
npm run build          # production build into build/ (fails on broken links and anchors)
npm run serve          # serve build/ locally
npm run typecheck      # tsc over the config and React pages
node scripts/check-mdx.mjs   # fast MDX syntax + capture check, no full build
```

From the repository root: `make docs` (build) and `make docs-serve` (dev server).

## Layout

| Path | Contents |
| --- | --- |
| `docs/` | The manual. `.md` is parsed as CommonMark; use `.mdx` for pages with `<Screenshot>` / `<Video>`. |
| `docs/reference/configuration.md` | **Generated**, git-ignored. Do not edit. |
| `blog/` | Release notes (one post per release). |
| `src/pages/index.tsx` | Landing page. `src/pages/api.tsx` renders `static/openapi.json` with Redoc. |
| `src/components/` | `Screenshot` (light/dark capture pair) and `Video`; registered for MDX in `src/theme/MDXComponents.tsx`. |
| `static/img/screens/` | UI screenshots, `<name>-light.png` and `<name>-dark.png`. |
| `static/video/` | Screen recordings, `<name>.webm` plus a `<name>.jpg` poster. |
| `static/openapi.json`, `static/schemas/dag-spec.schema.json` | Exported API and spec schemas. |
| `scripts/` | Build helpers (below). |
| `site.config.ts` | GitHub owner/repository, site URL, base URL and edit branch: the one place to change when the project moves. Each value can also be set with `DOCS_GITHUB_OWNER`, `DOCS_GITHUB_REPO`, `DOCS_URL`, `DOCS_BASE_URL`, `DOCS_EDIT_BRANCH`. |

## Generated content

* **Configuration reference.** `scripts/sync-reference.mjs` copies `../docs/configuration-reference.md`
  (generated from the `Settings` model by `scripts/gen_config_docs.py`) into `docs/reference/`, adding front matter and rewriting repository links to site routes. It runs
  automatically before `npm start` and `npm run build` (`prestart` / `prebuild`), so the site cannot
  drift from the code. A link to a repository doc it does not know fails the sync; add it to `LINKS`.
* **llms.txt.** `scripts/llms-txt.mjs` (a local plugin) writes `build/llms.txt` (index) and
  `build/llms-full.txt` (every page as Markdown, screenshots as their alt text) after each build,
  in sidebar order.
* **API and spec schemas.** `npm run export-api` runs `uv run autoair export-openapi` and
  `uv run autoair export-schema` and writes `static/openapi.json` and
  `static/schemas/dag-spec.schema.json`. It needs the repository's Python environment, so the outputs
  are committed and the site build does not need Python. Re-run it after API or spec changes.
* **Screenshots and videos.** `npm run capture` (or `capture:screens` / `capture:videos`) starts a
  private AutoAir (`scripts/capture/server.sh`: API on :8860, UI on :8861, scripted LLM on :8869),
  seeds deterministic demo data, and captures every screen at 1440×900 in light and dark mode. It
  needs an Airflow 3 API server on `AIRFLOW_URL` (default `http://localhost:8080`) whose
  `/auth/token` hands out tokens, such as `scripts/dev-airflow.sh`. See the header of
  `scripts/capture/run.mjs` for options. Keep `static/img/screens` + `static/video` under 40 MB.

## Writing

* Professional, specific English. Verify every setting name against the configuration reference and
  every UI label against `frontend/src/i18n/locales`.
* Link with site routes (`/docs/admin/iam#roles`), not file names. `onBrokenLinks`,
  `onBrokenAnchors` and broken Markdown links/images all fail the build.
* New pages must be added to `sidebars.ts`.
* Mark statements that still need confirmation with `<!-- TODO(coordinator): … -->` in `.md` or
  `{/* TODO(coordinator): … */}` in `.mdx`, and find them with `grep -rn "TODO(coordinator)" docs`.
* Screenshots: `<Screenshot name="deploy-dialog" alt="…" caption="…" />`; the name must exist in
  both modes (`node scripts/check-mdx.mjs` reports missing captures).

## Versioning

The docs are unversioned until the first release. To freeze a version:

```bash
npm run docs:version -- 0.1
```

This creates `versioned_docs/`, `versioned_sidebars/` and `versions.json`; the navbar shows a version
dropdown as soon as `versions.json` exists.

## Search

Local search (`@easyops-cn/docusaurus-search-local`) indexes docs and release notes at build time; it
works offline and on GitHub Pages. Setting `ALGOLIA_APP_ID`, `ALGOLIA_API_KEY` and
`ALGOLIA_INDEX_NAME` at build time switches to Algolia DocSearch.
