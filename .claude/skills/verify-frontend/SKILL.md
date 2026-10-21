---
name: verify-frontend
description: Run AutoAir's frontend checks in fast-to-slow order (codegen drift, typecheck, eslint, prettier, vitest, build, Playwright). Use before reporting any change under frontend/ as done.
---

# Verify a frontend change

All commands run in `frontend/`.

```bash
cd frontend
npm run codegen && git diff --stat -- openapi.json src/api/gen   # only if backend API changed
npm run typecheck              # tsc --noEmit -p tsconfig.json
npm run lint                   # eslint .
npm run format:check           # prettier --check src e2e   (fix: npm run format)
npx vitest run src/<area>      # focused; then: npm test
```

Then, by kind of change:

| Change | Check |
|---|---|
| Store selectors / zustand | `npx vitest run src/store` (`selectors.test.ts` rejects selectors that build new objects) |
| Undo/redo | `npx vitest run src/store/history.test.ts`; E2E `e2e/undo.spec.ts` |
| i18n strings | skill i18n-strings; `npx vitest run src/i18n/parity.test.ts` (all namespaces) |
| Anything visible | skill verify-ui-screenshots (light + dark, plugin + standalone) |
| Screens covered by the visual suite | `npm run test:visual` (skill update-visual-baselines when intended) |
| Menus, dialogs, inspectors | crawler on a private stack (skill run-e2e) |
| Bundle | `npx vite build --mode standalone --outDir "$(mktemp -d)"` and `--mode plugin` succeed; ELK appears only in `assets/layout.worker-*.js` |
| Canvas auto-layout | `npx vitest run src/canvas/layout.test.ts`; E2E `e2e/auto-layout.spec.ts` (worker and no-worker fallback) |
| E2E spec that deploys DAGs | `DeployedDags` from `e2e/cleanup.ts` deletes them in `afterAll` |

## Pitfalls

- `npm run build` writes `src/autoair/static/{plugin,standalone}`, which the shared demo
  serves. Build into a private `--outDir` unless you mean to update the demo.
- Do not run `npm run dev` on :5173: the shared Vite server already owns it. Use
  `npx vite --mode standalone --port <free> --strictPort` with `AUTOAIR_API_PORT=<your API>`.
- Never hand-edit `src/api/gen/` or `openapi.json`; CI regenerates and diffs them.
- Component tests opt into jsdom with `// @vitest-environment jsdom`; the default
  environment is Node. Render through `src/test/render.tsx`.
- Failures in files you did not touch are probably another agent's work in progress.

## Done when

- [ ] typecheck, lint, format:check, vitest pass
- [ ] codegen output committed with the backend change, if any
- [ ] UI changes: screenshots taken (light/dark x plugin/standalone)
