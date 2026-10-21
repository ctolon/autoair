---
name: i18n-strings
description: Add or change user-visible text in the AutoAir frontend with English and Turkish translations (i18next namespaces under frontend/src/i18n/locales). Use for any new label, message, tooltip, error text or problem description in frontend/src.
---

# Add i18n strings

Every user-visible string exists in **English and Turkish**. Instance: `frontend/src/i18n/index.ts`
(`fallbackLng: "en"`; language from localStorage `autoair.lang`, then Airflow's `i18nextLng`,
then the browser).

Namespaces (`FEATURE_NAMESPACES`), one JSON file per language:

```
frontend/src/i18n/locales/{en,tr}/{common,canvas,inspector,lifecycle,admin,ai}.json
```

## Steps

1. Pick the namespace of the feature (`useTranslation("inspector")`); shared words go to
   `common`. Reuse an existing key when the meaning is identical.
2. Add the key to **both** `en/<ns>.json` and `tr/<ns>.json`, at the same path. Nest by
   component or feature (`"deploy": {"title": ...}`); keep keys stable and descriptive.
3. Interpolation uses `{{name}}`; plurals use i18next suffixes (`_one`, `_other`). Never
   build sentences by concatenating translated fragments.
4. Problem messages from the backend are translated through
   `frontend/src/lib/problemMessages.ts` (`problemText(p)`, keys `lifecycle:problemText.<key>`);
   unknown codes fall back to the backend message. Other server texts shown in the UI come
   with a stable code plus params next to the English text (troubleshoot action results:
   `code`/`params` -> `ai:troubleshoot.result.<code>`, `resultText()` in
   `src/ai/AirflowActions.tsx`; KB source `reason_code` -> `admin:kb.reason.<code>`): add the
   key for every new code in en and tr, and keep the English `message` as the fallback.
5. Turkish: proper characters (ı, ş, ğ, ü, ö, ç, İ), sentence case like the English. If you
   are unsure of a translation, keep the English meaning exact and flag it in your report
   rather than guessing.
6. Check:

   ```bash
   cd frontend
   npx vitest run src/i18n/parity.test.ts   # every namespace: same keys, same {{vars}}, no empty tr
   npm run typecheck && npm run lint
   ```

   `parity.test.ts` reads every `src/i18n/locales/{en,tr}/*.json`, so a new namespace is
   covered as soon as its files exist (list it in `FEATURE_NAMESPACES` too).

7. Look at the UI in Turkish (`--lang tr` in skill verify-ui-screenshots): Turkish strings
   are often longer; nothing may overflow or truncate without a tooltip.

## Done when

- [ ] Key present in en and tr; `src/i18n/parity.test.ts` passes
- [ ] No hard-coded user-visible text left in the component
