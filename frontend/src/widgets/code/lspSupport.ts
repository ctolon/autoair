import { useEffect, useState } from "react";

import { useAccess } from "../../lib/access";
import type { LspLanguage } from "./lspClient";

type LspModule = typeof import("./lspClient");

/**
 * The language-server client module when `features.ide_language_server` is on and the server
 * has a language server for `language` installed; null otherwise (the editor keeps its
 * built-in services: jedi completions for Python, completions + ShellCheck for bash). The
 * client code loads only when needed.
 */
export function useLanguageServer(language: LspLanguage | null): LspModule | null {
  const enabled = useAccess().feature("ide_language_server") && language !== null;
  const [state, setState] = useState<{ language: LspLanguage; module: LspModule } | null>(null);
  useEffect(() => {
    if (!enabled || !language) return;
    let cancelled = false;
    void import("./lspClient")
      .then(async (module) => {
        const available = await module.serverAvailability();
        if (!cancelled && available[language]) setState({ language, module });
      })
      .catch(() => {
        /* the built-in services stay */
      });
    return () => {
      cancelled = true;
    };
  }, [enabled, language]);
  return enabled && state && state.language === language ? state.module : null;
}
