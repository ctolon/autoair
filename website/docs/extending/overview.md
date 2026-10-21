---
title: Extending AutoAir
description: The extension points of AutoAir - operator discovery, catalog sources, custom node kinds, templates and the marketplace, the REST API, the DAG spec and MCP.
sidebar_label: Overview
---

AutoAir is built to be extended without forking it. Most extensions are plain Python packages installed next to AutoAir and found through entry points; others are data (templates, marketplace indexes) or external clients (REST API, MCP). The table below links each extension point to its full guide.

## Extension points

| You want to | Mechanism | Code runs where | Guide |
|---|---|---|---|
| Show your company's operators in the palette | Settings: `extra_operator_packages`, `extra_operator_modules`, `extra_operator_distributions`, `extra_operator_paths`, `custom_operator_paths` | Nowhere: operators are analysed statically | [Custom operators](/docs/extending/custom-operators) |
| Give a package's operators palette groups, icons, parameter widgets and examples | `autoair.yaml` in the package or entry point `autoair.operator_metadata`; settings `catalog.groups`, `catalog.param_overrides`, `icons.rules_extra` | Nowhere: metadata is read statically (entry points are loaded only with `catalog_strategy=import`) | [Operator metadata](/docs/extending/operator-metadata) |
| Feed the catalog from your own module list | Entry point `autoair.catalog_sources` (a `CatalogSource`) | AutoAir server, at catalog build time | [Catalog sources](/docs/extending/catalog-sources) |
| Add a node kind with its own form and code generation | Entry point `autoair.node_kinds` (a `NodeKindHandler`), optional frontend `registerNodeKind()` | AutoAir server (code generation emits AST only) | [Custom node kinds](/docs/extending/node-sdk) |
| Share DAG templates and fragments across teams | Signed marketplace index (`autoair marketplace keygen/build`) | Nothing executes; templates are data | [Publishing a marketplace](/docs/extending/marketplace) |
| Automate DAG management from CI or another tool | REST API (`/api` or `/autoair/api`) | Your client | [REST API](/docs/extending/rest-api) |
| Generate or check specs outside AutoAir | DagSpec JSON Schema (`autoair export-schema`) | Your tooling | [Spec format](/docs/extending/spec-format) |
| Let AI agents build and deploy DAGs | MCP server (`/mcp`, `autoair mcp`) | AutoAir server | [MCP integration](/docs/extending/mcp-integration) |
| Enforce organisation rules | Policy settings | AutoAir validation | [Policy](/docs/admin/policy) |

## Principles shared by every extension

- **Nothing is imported to discover operators.** The catalog parses provider and custom operator code with `ast`; classes that cannot be resolved statically are introspected in a subprocess. A broken operator module cannot crash the server.
- **Code generation emits AST, never source text.** Custom node kinds return `ast.expr` nodes, so values typed by users cannot inject code into the generated DAG file.
- **An extension must never break the builder.** Entry points that fail to load are logged (`failed to load autoair node kind ...`, `failed to load catalog source ...`) and skipped; a failing validation hook is reported as one validation message.
- **No code is installed at runtime.** Marketplace `extension` entries only show a `pip install` line. An administrator installs Python packages in the image and restarts.
- **Whatever the generated DAG imports must exist where Airflow runs.** Install provider packages, operator libraries and node-kind packages on the DAG processor, scheduler and workers as well as on the AutoAir server.

## Where to install extension packages

| Deployment | Install into |
|---|---|
| Plugin | The Airflow image used by the API server (and, for anything the generated DAGs import, every Airflow component) |
| Standalone | The environment of `autoair serve`, plus the Airflow image for runtime imports |
| Separate AI service | Also the AI service image, so AI tools see the same catalog and node kinds |

After installing, restart AutoAir. If you precompute the catalog (`AUTOAIR_CATALOG_FILE`), rebuild it in the image with `autoair catalog build`; administrators can also rebuild the running catalog from the [admin panel](/docs/admin/admin-panel) (`POST /api/catalog/refresh`, permission `catalog:refresh`).

## Templates and building blocks

Built-in starter DAGs and fragments ship in `autoair/templates/builtin.py`. Users save their own DAG templates, reusable fragments and operator templates from the UI ([Templates and fragments](/docs/user-guide/templates-and-fragments), [Operator templates](/docs/user-guide/operator-templates)); they are stored at `AUTOAIR_TEMPLATES_URL` or in the database. Templates are data, not code, and can be distributed through a marketplace.

## Related pages

- [Configuration reference](/docs/reference/configuration)
- [Security overview](/docs/security/overview)
- [Development](/docs/contributing/development)
