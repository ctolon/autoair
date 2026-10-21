import { useEffect, useRef } from "react";

import { isEditable } from "../canvas/useCanvasCommands";
import { findCommandForEvent, getCommands } from "../lib/commands";

/**
 * Global keyboard dispatcher: runs the registered command bound to a key press.
 *
 * - ignored while typing (inputs, textareas, selects, contenteditable, CodeMirror);
 * - only for events inside the builder or with no particular focus (document body), so the
 *   host page (Airflow) and portalled dialogs/menus keep their own keys;
 * - runs after React handlers (bubble phase on window) and skips events they already
 *   handled (`defaultPrevented`, e.g. React Flow moving a focused node with the arrows).
 */
export function KeyboardShortcuts() {
  const anchor = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented || e.isComposing) return;
      if (isEditable(e.target)) return;
      const root = anchor.current?.parentElement ?? null;
      const target = e.target instanceof Node ? e.target : null;
      const inScope =
        target === null ||
        target === document.body ||
        target === document.documentElement ||
        (!!root && root.contains(target));
      if (!inScope) return;
      const cmd = findCommandForEvent(e, getCommands());
      if (!cmd) return;
      e.preventDefault();
      // Handled once: other window keydown listeners must not run the same shortcut again.
      e.stopImmediatePropagation();
      void cmd.run();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  return <span ref={anchor} hidden aria-hidden />;
}
