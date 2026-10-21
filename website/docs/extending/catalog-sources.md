---
title: Catalog sources
description: Feed AutoAir's operator catalog from your own code through the autoair.catalog_sources entry point.
sidebar_label: Catalog sources
---

A **catalog source** tells AutoAir which Python modules contain operators. The built-in sources cover installed providers, Airflow's folders, configured folders, packages, distributions, modules and files (see [Custom operators](/docs/extending/custom-operators)). When those settings cannot express what you need, for example a module list that comes from an internal registry, or provider and integration names you want shown in the palette, register your own source through the `autoair.catalog_sources` entry point.

A catalog source only **names modules**. AutoAir still parses those modules statically and builds the forms itself; your source never returns classes or schemas.

## The contract

```python
from collections.abc import Iterable
from typing import Protocol

from autoair.catalog.service import ModuleRef


class CatalogSource(Protocol):
    def modules(self) -> Iterable[ModuleRef]: ...
```

`ModuleRef` is a frozen dataclass:

| Field | Type | Meaning |
|---|---|---|
| `module` | `str` | Dotted module name, for example `acme_ops.operators.jira`. It must be locatable on `sys.path` without importing it |
| `provider` | `str` | Name shown as the operator's provider in the palette and catalog |
| `category` | `"operator"`, `"sensor"` or `"transfer"` | Palette category of the module's classes |
| `provider_version` | `str` or `None` | Version shown with the provider |
| `integration` | `str` or `None` | Integration name (used for logos and grouping), for example `Jira` |

## Example

```python
# acme_autoair/catalog.py
from __future__ import annotations

from collections.abc import Iterable
from importlib.metadata import version

from autoair.catalog.service import ModuleRef

_MODULES = {
    "acme_ops.operators.jira": ("operator", "Jira"),
    "acme_ops.sensors.jira": ("sensor", "Jira"),
    "acme_ops.transfers.jira_to_s3": ("transfer", "Jira → Amazon S3"),
}


class AcmeOperators:
    """Operators of the internal acme-airflow-ops library."""

    def modules(self) -> Iterable[ModuleRef]:
        provider_version = version("acme-airflow-ops")
        for module, (category, integration) in sorted(_MODULES.items()):
            yield ModuleRef(module, "acme-airflow-ops", category, provider_version, integration)
```

Register it in the package's `pyproject.toml`:

```toml
[project.entry-points."autoair.catalog_sources"]
acme = "acme_autoair.catalog:AcmeOperators"
```

The entry point may reference a class (AutoAir instantiates it without arguments) or an instance.

Install the package into AutoAir's environment and restart (or rebuild the catalog). The operators appear in the palette with `acme-airflow-ops` as provider.

## Rules

- **Keep `modules()` cheap, deterministic and side-effect free.** AutoAir calls it for every catalog build and also to compute the catalog's cache key. It must not import the modules it names, call the network, or return a different order between calls.
- **Name modules, not packages to walk.** The source yields individual modules. To scan a whole package, the `extra_operator_packages` setting is simpler.
- **Exceptions are contained.** If the entry point fails to load, AutoAir logs `failed to load catalog source '<name>'` and continues with the other sources. If `modules()` raises while the cache key is computed, the error type becomes part of the key and the catalog is built without that source's modules.
- **Discovery filters apply.** The [discovery filters](/docs/extending/custom-operators#discovery-filters) apply to modules from your source as well: `discovery.skip_module_pattern` / `skip_module_patterns_extra` (skipped modules), `discovery.skip_class_pattern` / `skip_class_patterns_extra` (skipped classes), `discovery.class_hint_pattern` / `class_hint_patterns_extra` (text pre-filter) and `discovery.max_file_bytes` (file size cap).
- **Only task classes are listed.** Within each module AutoAir lists classes that resolve to operators, sensors or notifiers; helper classes are ignored.

## Caching

For custom sources the cache key contains each yielded module's name, provider, category, version, integration, and the size and modification time of its file. Adding, removing or editing a module therefore produces a new catalog on the next build. A precomputed catalog file (`AUTOAIR_CATALOG_FILE`) built before the source was installed is ignored, because its key no longer matches; rebuild it with `autoair catalog build` in the image.

## Runtime requirements

The modules your source names are imported by the generated DAG files. Install the operator library on every Airflow component that parses or runs DAGs, not only next to AutoAir. The catalog source package itself is only needed where AutoAir runs.

## Debugging

```bash
python -c "from autoair.catalog.service import entry_point_sources; print([list(s.modules()) for s in entry_point_sources()])"
autoair catalog dump | jq -r '.[] | select(.provider == "acme-airflow-ops") | .fqn'
```

`GET /api/catalog/stats` lists discovery errors (modules that could not be parsed or resolved).

## Related pages

- [Custom operators](/docs/extending/custom-operators)
- [Custom node kinds](/docs/extending/node-sdk)
- [Extending AutoAir](/docs/extending/overview)
