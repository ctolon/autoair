---
name: add-operator-widget
description: Add or change how an operator parameter is edited in the inspector - a new widget, a new param type mapping, a code renderer, or a catalog hint (x-autoair-widget, x-autoair-renderer). Use when touching catalog/static.py param schemas, ParamWidgetName in sections.py, frontend/src/lib/formSchema.ts or frontend/src/widgets/ParamsForm.tsx.
---

# Add an operator widget or param type

Operator parameters travel as JSON Schema with `x-autoair-*` extensions from the backend
catalog to the inspector. The pipeline:

1. **Backend schema** (`src/autoair/catalog/static.py`, `_param`; the import strategy in
   `catalog/introspection.py`): sets `x-autoair-annotation`, `x-autoair-templated`,
   `x-autoair-renderer` (from template renderers or name hints `_RENDERER_HINTS`),
   `x-autoair-widget` (`connection` from `_CONN_RE`, `pool`), and
   `x-autoair-python-ref = "required"` for types JSON cannot express.
   `catalog/service.py` adds `x-autoair-conn-type`.
2. **Admin overrides**: `ParamOverride` and `ParamWidgetName` in `src/autoair/sections.py`;
   `catalog/metadata.py::apply_param_overrides` applies them (`CODE_WIDGETS` get a renderer).
3. **Frontend mapping**: `frontend/src/lib/formSchema.ts`, `type ParamWidget` and
   `widgetFor(prop)`; `CODE_RENDERERS` decides code vs text for strings.
4. **Rendering**: `frontend/src/widgets/ParamsForm.tsx` switches on `field.widget`; primitives
   live in `frontend/src/widgets/fields.tsx`, value editors in `ValueEditors.tsx`, Airflow
   pickers in `AirflowPickers.tsx`, CodeMirror in `widgets/code/` (lazy, via `LazyEditor`).

## Steps

1. Decide whether the hint is derived (catalog), configured (override), or both. Derived hints
   must not import the operator module: the static strategy parses source only.
2. Backend: add the hint and, for an override value, extend `ParamWidgetName`; run
   `uv run python scripts/gen_config_docs.py`. Test in `tests/unit/test_catalog_static.py`.
3. Frontend: extend `ParamWidget`, `widgetFor`, and the `ParamsForm` switch. Every param must
   still accept a literal, a Jinja template and an XCom reference.
4. The widget must render any value it can receive, including wrong types from an old spec,
   a template string where a number is expected, `null`, or an in-progress edit. Show a
   problem instead of throwing; the inspector sits inside an `ErrorBoundary`
   (`frontend/src/ui/ErrorBoundary.tsx`) but a crash there still costs the user the panel.
5. Strings: i18n keys in `frontend/src/i18n/locales/{en,tr}/inspector.json`. Colours: Chakra
   semantic tokens (`fg.muted`, `bg.panel`, `border.emphasized`), never palette values.
6. Tests: `frontend/src/lib/formSchema.test.ts` for the mapping; a jsdom component test
   (`// @vitest-environment jsdom`, helper `src/test/render.tsx`) feeding invalid values.
7. Verify in the browser (skill `verify-ui-screenshots`) and run the crawler for the
   inspector if the change is broad.

## Done when

- [ ] Backend hint + test; config docs regenerated if `ParamWidgetName` changed
- [ ] `formSchema.test.ts` and an invalid-input component test pass
- [ ] en + tr strings; light and dark screenshots in plugin and standalone mode
