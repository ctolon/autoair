import i18next, { type i18n as I18n } from "i18next";
import { initReactI18next } from "react-i18next";

/**
 * Translations are split by feature namespace (one JSON file per namespace and locale)
 * so features can be developed independently. English is the source language and the
 * fallback; keys missing in other locales fall back to English.
 *
 * The language follows Airflow's UI (it stores its choice in localStorage
 * `i18nextLng`), then the browser.
 */
const modules = import.meta.glob<{ default: Record<string, unknown> }>("./locales/*/*.json", {
  eager: true,
});

/**
 * Feature namespaces (``locales/<lng>/<ns>.json``, loaded by the glob above): every locale
 * must provide each of them; a missing file falls back to English key by key.
 */
export const FEATURE_NAMESPACES = ["common", "canvas", "inspector", "lifecycle", "admin", "ai"] as const;

const resources: Record<string, Record<string, Record<string, unknown>>> = {};
for (const [path, mod] of Object.entries(modules)) {
  const match = /\.\/locales\/([^/]+)\/([^/]+)\.json$/.exec(path);
  if (!match) continue;
  const [, lng, ns] = match as unknown as [string, string, string];
  resources[lng] ??= {};
  resources[lng][ns] = mod.default;
}

export const SUPPORTED_LANGUAGES = Object.keys(resources).sort();
export const NAMESPACES = [...new Set(Object.values(resources).flatMap((r) => Object.keys(r)))];

function detectLanguage(): string {
  const candidates: string[] = [];
  try {
    const stored = localStorage.getItem("autoair.lang") ?? localStorage.getItem("i18nextLng");
    if (stored) candidates.push(stored);
  } catch {
    /* storage unavailable */
  }
  candidates.push(...(navigator.languages ?? [navigator.language]));
  for (const c of candidates) {
    const base = c.toLowerCase().split("-")[0] ?? "en";
    if (resources[base]) return base;
  }
  return "en";
}

// A dedicated instance: inside Airflow the host has its own i18next instance.
export const i18n: I18n = i18next.createInstance();
void i18n.use(initReactI18next).init({
  resources,
  lng: detectLanguage(),
  fallbackLng: "en",
  ns: NAMESPACES,
  defaultNS: "common",
  interpolation: { escapeValue: false },
  returnNull: false,
});

export function setLanguage(lng: string): void {
  void i18n.changeLanguage(lng);
  try {
    localStorage.setItem("autoair.lang", lng);
  } catch {
    /* ignore */
  }
}
