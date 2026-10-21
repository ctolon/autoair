---
title: Knowledge base sources and versions
description: Which documentation the knowledge base indexes - components, installed or all providers, version selection, the source kinds and formats - and how refreshes stay incremental.
sidebar_label: Sources and versions
---

What the [knowledge base](/docs/ai/knowledge-base) indexes is the product of three settings:
**components** (which documentation), **versions** (which release of it) and **sources** (where
it is read from). Every field is in the [`kb` settings reference](/docs/reference/configuration#kb).

## Components

`kb.components` (JSON list) selects the documentation sets:

| Component | Documentation | Default | Docs site package | Repository folder (apache/airflow) |
|---|---|---|---|---|
| `core` | Apache Airflow: concepts, how-tos, configuration, CLI, REST API, upgrades | on | `apache-airflow` | `airflow-core/docs` |
| `task_sdk` | Task SDK (`airflow.sdk`) | on | `task-sdk` | `task-sdk/docs` |
| `providers` | Provider packages: operators, hooks, sensors, connections, plus the general provider docs | on | `apache-airflow-providers-<name>`, `apache-airflow-providers` | `providers/<name>/docs` |
| `helm_chart` | The official Helm chart | on | `helm-chart` | `chart/docs` |
| `airflowctl` | The airflowctl remote CLI | on | `apache-airflow-ctl` | `airflow-ctl/docs` |
| `astronomer_airflow` | Astronomer's Airflow tutorials, concepts and guides (formerly Learn) | on | `astronomer.io/docs/airflow/llms.txt` | - |
| `astro_cli` | Astronomer's Astro CLI | off | `astronomer.io/docs/cli/llms.txt` | - |
| `astro` | Astro, Astronomer's managed platform | off | `astronomer.io/docs/astro/llms.txt` | - |
| `astro_runtime` | Astro Runtime | off | `astronomer.io/docs/runtime/llms.txt` | - |

The default covers running and authoring Airflow itself plus Astronomer's vendor-neutral guides;
Astronomer's product documentation is opt-in. Astronomer's pages are published under
Astronomer's terms, the Apache pages under the Apache License 2.0; check that indexing them
internally fits your organisation's policy.

A source may narrow or widen this list with its own `components`, and admins can change it per
source at runtime (admin panel, `PUT /api/kb/sources/{id}`).

### Installed or all providers

`kb.providers` decides which provider packages are indexed:

- `installed` (default): the `apache-airflow-providers-*` distributions installed in AutoAir's
  environment, at their installed versions. This matches the code AutoAir's agents may write
  (they only import what is installed), the version-exact docstrings that research already
  indexes, and it keeps the index a fraction of the size: there are more than 90 provider
  packages.
- `all`: every provider listed on the docs site's packages page
  (`apache-airflow-providers/stable/packages-ref.html`), or every `provider.yaml` of a
  repository checkout; providers that are not installed are indexed from their `stable` docs.

`kb.provider_include` (default `["*"]`) and `kb.provider_exclude` filter by name with fnmatch
patterns, on the short name (`amazon`, `common-sql`) or the full distribution name.

## Versions

`kb.versions` (default `["installed"]`) applies to every component; a source may override it.

| Entry | Meaning |
|---|---|
| `installed` | The installed version of the component's distribution (`apache-airflow`, `apache-airflow-task-sdk`, `apache-airflow-ctl`, each provider). When the package is not installed (the Helm chart has none), `stable` is used; when the docs of the installed version are not published, `stable` is used too and the status records an `info` message |
| `stable` | The docs site's `stable` alias |
| an explicit version, e.g. `3.1.0` | That release. Applies to the non-provider components; provider versions differ from Airflow's, so providers only honour `installed` and `stable` |

Several entries index several versions side by side; search can filter by `version`. When the
installed version changes (an Airflow upgrade), the next refresh indexes the new version and
removes the pages of the old one.

For Astronomer components, `installed` and `stable` mean the `current` documentation of the
`llms.txt` index; an explicit entry (`v1.45`) selects the entry of the index's *Versions* list
whose label contains it.

## Source kinds

`kb.sources` is a JSON list; the default is `[{"id": "official", "kind": "official"}]`.

| Kind | Reads | Needs |
|---|---|---|
| `official` | airflow.apache.org (Sphinx sites) and astronomer.io (`llms.txt` indexes of Markdown pages) | `kb.online=true` |
| `site` | A mirror of `airflow.apache.org/docs/` over HTTP, same layout (`<package>/<version>/searchindex.js`) | `url` |
| `llms_txt` | An `llms.txt` index of Markdown pages (Astronomer layout, or your own) | `url`; the component is the first of `components`, else `component` |
| `git` | A shallow checkout of a repository | `url` or `conn_id`; optional `ref` |
| `path` | A local folder or an object-storage URL (`s3://`, `gs://`, `abfs://`) | `url`; optional `conn_id` |
| `bundle` | A KB bundle made with `autoair kb build` (pages already parsed) | `url` (file path or fsspec URL); optional `conn_id` |

Only `official` depends on `kb.online`: the other kinds are explicitly configured by an admin and
are read whether or not the server may reach the internet. Every kind keeps its pages under its
`id`, so renaming a source re-indexes it. Common fields:

| Field | Use |
|---|---|
| `enabled` | Index the source (the admin panel can switch it) |
| `title` | Display name |
| `components`, `versions` | Override `kb.components` / `kb.versions` for this source |
| `include`, `exclude` | fnmatch patterns on the page path (Sphinx page name, file path or URL path) |
| `layout` | `git` / `path`: `airflow_repo`, `sphinx_site`, `files` or `auto` |
| `format` | Sphinx pages: `html` or `sources` (`auto` = `html`) |
| `public_base_url` | Base of citation URLs for `files` and repository pages |
| `allow_private` | `site` / `llms_txt`: the host may resolve to a private address |
| `storage_options` | `path` / `bundle` without `conn_id`: non-secret fsspec options |

Folder layouts (`git`, `path`):

- `airflow_repo`: the apache/airflow repository. Each enabled Apache component is read from its
  folder (table above); versions come from the checkout (`<project>/pyproject.toml`,
  `chart/Chart.yaml`, each provider's `provider.yaml`), not from `kb.versions`. Citation URLs
  point to the matching page of `public_base_url` (default `https://airflow.apache.org/docs/`).
  `exampleinclude`, `literalinclude` and `include` directives are resolved inside the checkout,
  so the example code is indexed; paths that leave the checkout are ignored.
- `sphinx_site`: a downloaded copy of the docs site (`<package>/<version>/searchindex.js` and the
  HTML pages), read like the online site.
- `files`: any `.md`, `.mdx`, `.markdown`, `.rst`, `.txt`, `.html`, `.htm` files (dot folders
  skipped). The component is the first of `components`, else `component` (default `custom`); the
  version is the first explicit entry of the source's `versions`, else `unversioned`. Citations use
  `public_base_url` + the relative path, or `kb://<source>/<path>`.
- `auto`: `airflow_repo` when `airflow-core/docs` or `providers/` exists, `sphinx_site` when a
  `*/*/searchindex.js` exists, otherwise `files`.

### Page formats

Sphinx sites are listed through their `searchindex.js` (page names and titles). With `format`
`html` (the default) AutoAir reads the rendered pages, which contain the example code that
`exampleinclude` pulls in from the provider system tests. `sources` reads the
`_sources/<page>.rst.txt` files instead: smaller downloads, but without the included examples.

The generated API reference (`_api/...` pages) is skipped unless
`kb.fetch.include_api_reference` is true: research already indexes the docstrings of the
installed packages, and the API reference would multiply the size of the index.
`kb.fetch.exclude_pages` (default `genindex`, `search`, `py-modindex`, `_modules/*`) drops
index pages.

## Refreshes

| Trigger | How |
|---|---|
| Schedule | Every replica checks once a minute (first check 30 s after start). A refresh starts when `kb.refresh_interval_hours` (24; 0 disables) passed since the last one, or, with `kb.refresh_on_start`, when the index is empty and no refresh ran yet (a failed first refresh is retried hourly) |
| Admin panel / API | `POST /api/kb/refresh` with `mode` `refresh` or `rebuild` and optional `sources`; `POST /api/kb/cancel` stops it |
| CLI | `autoair kb refresh [--rebuild] [--source ID]` |

A refresh is incremental: unchanged units are skipped by fingerprint, unchanged pages answer
`304 Not Modified` or have the same content hash. `rebuild` drops the selected sources (or
everything) first. Changing `kb.chunk.*` re-chunks the stored pages, and changing the embedding
model re-embeds them, both without fetching again.

Limits per refresh: `kb.fetch.max_pages` (25000) in total, `kb.fetch.max_pages_per_package`
(3000) per package version, `kb.fetch.max_run_seconds` (4 h). A run that hits a limit keeps what
it indexed, removes nothing and reports `partial`. Failed pages keep their previous version and
are retried next time.

The status (`GET /api/kb/status`, `autoair kb status`) shows the outcome (`ok`, `partial`,
`failed`, `cancelled`), per-source counts (units, pages seen, changed, unchanged, removed,
failed, chunks written) and up to `kb.max_errors` (50) messages with their level
(`error`, `warning`, `info`).

## Examples

Online, defaults plus the Astro CLI, two Airflow versions:

```bash
AUTOAIR_KB__ONLINE=true
AUTOAIR_KB__COMPONENTS='["core","task_sdk","providers","helm_chart","airflowctl","astronomer_airflow","astro_cli"]'
AUTOAIR_KB__VERSIONS='["installed","3.1.0"]'
```

Only a few providers, every one of them whether installed or not:

```bash
AUTOAIR_KB__PROVIDERS=all
AUTOAIR_KB__PROVIDER_INCLUDE='["amazon","google","cncf-kubernetes","common-*"]'
```

More source configurations are in [Air-gapped setup](/docs/ai/knowledge-base-air-gapped).
