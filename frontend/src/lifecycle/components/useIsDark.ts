import { useSyncExternalStore } from "react";

function isDark(): boolean {
  const root = document.documentElement;
  return root.classList.contains("dark") || root.dataset.theme === "dark" || root.style.colorScheme === "dark";
}

function subscribe(onChange: () => void): () => void {
  const observer = new MutationObserver(onChange);
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ["class", "data-theme", "style"] });
  return () => observer.disconnect();
}

/** Follows the Chakra / Airflow colour mode (class or data-theme on <html>). */
export function useIsDark(): boolean {
  return useSyncExternalStore(subscribe, isDark, () => false);
}
