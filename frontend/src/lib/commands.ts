import { type ReactNode, useSyncExternalStore } from "react";

/**
 * Global command registry backing the command palette (Ctrl/⌘+K) and keyboard shortcuts.
 * Features register commands at module load or from components (return value unregisters).
 *
 * Keyboard shortcuts are dispatched by `src/shell/KeyboardShortcuts.tsx`: it runs the first
 * registered command whose shortcut matches, unless focus is in a text input / editor.
 */
export type Command = {
  id: string;
  /** i18n-resolved title shown in the palette */
  title: string;
  group?: string;
  /** e.g. "mod+k", "mod+shift+z", "delete" (mod = Ctrl on Linux/Windows, ⌘ on macOS) */
  shortcut?: string;
  /** Additional shortcuts that run the same command (not shown in the palette). */
  altShortcuts?: string[];
  /**
   * The shortcut is only displayed: the browser's native event handles it (e.g. copy/paste
   * go through `copy`/`paste` events, which need no clipboard permission).
   */
  handledNatively?: boolean;
  /** Hide from the command palette (still listed in the shortcuts sheet). */
  hidden?: boolean;
  /** Let the dispatcher run the command repeatedly while the key is held (e.g. nudging). */
  repeat?: boolean;
  icon?: ReactNode;
  keywords?: string[];
  when?: () => boolean;
  run: () => void | Promise<void>;
};

type Listener = () => void;
const commands = new Map<string, Command>();
const listeners = new Set<Listener>();
let snapshot: Command[] = [];

function emit(): void {
  snapshot = [...commands.values()];
  for (const l of listeners) l();
}

export function registerCommands(...items: Command[]): () => void {
  for (const c of items) commands.set(c.id, c);
  emit();
  return () => {
    for (const c of items) if (commands.get(c.id) === c) commands.delete(c.id);
    emit();
  };
}

export function getCommands(): Command[] {
  return snapshot;
}

export function getCommand(id: string): Command | undefined {
  return commands.get(id);
}

function subscribeCommands(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** React hook: the live list of registered commands. */
export function useCommandList(): Command[] {
  return useSyncExternalStore(subscribeCommands, getCommands, getCommands);
}

export function isCommandEnabled(cmd: Command): boolean {
  try {
    return !cmd.when || cmd.when();
  } catch {
    return false;
  }
}

export function runCommand(id: string): void {
  const cmd = commands.get(id);
  if (cmd && isCommandEnabled(cmd)) void cmd.run();
}

const isMac = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform);

export type KeyLike = Pick<KeyboardEvent, "key" | "ctrlKey" | "metaKey" | "shiftKey" | "altKey">;

const KEY_ALIASES: Record<string, string> = {
  esc: "escape",
  del: "delete",
  space: " ",
  left: "arrowleft",
  right: "arrowright",
  up: "arrowup",
  down: "arrowdown",
  plus: "+",
};

/** True when a keyboard event matches a shortcut like "mod+shift+z" ("+" itself is spelt "plus"). */
export function matchesShortcut(e: KeyLike, shortcut: string, mac: boolean = isMac): boolean {
  const parts = shortcut.toLowerCase().split("+");
  const raw = parts.pop() ?? "";
  const key = KEY_ALIASES[raw] ?? raw;
  const want = new Set(parts);
  const mod = mac ? e.metaKey : e.ctrlKey;
  // The "other" platform modifier must not be held (Ctrl+K on macOS is not ⌘K).
  const otherMod = mac ? e.ctrlKey : e.metaKey;
  if (want.has("mod") !== mod) return false;
  if (otherMod && !want.has("ctrl") && !want.has("meta")) return false;
  if (want.has("alt") !== e.altKey) return false;
  const pressed = e.key.toLowerCase();
  // Symbols like "?" are typed with Shift on most layouts: Shift is implied for them.
  const isSymbol = key.length === 1 && !/[a-z0-9 ]/.test(key);
  if (!isSymbol && want.has("shift") !== e.shiftKey) return false;
  return pressed === key;
}

/** All shortcuts (primary first) of a command. */
export function shortcutsOf(cmd: Command): string[] {
  return [...(cmd.shortcut ? [cmd.shortcut] : []), ...(cmd.altShortcuts ?? [])];
}

/** First enabled command bound to the event, ignoring natively handled shortcuts. */
export function findCommandForEvent(e: KeyLike & { repeat?: boolean }, list: Command[], mac: boolean = isMac) {
  for (const cmd of list) {
    if (cmd.handledNatively) continue;
    if (e.repeat && !cmd.repeat) continue;
    if (!shortcutsOf(cmd).some((s) => matchesShortcut(e, s, mac))) continue;
    if (isCommandEnabled(cmd)) return cmd;
  }
  return undefined;
}

const KEY_LABELS: Record<string, string> = {
  arrowleft: "←",
  arrowright: "→",
  arrowup: "↑",
  arrowdown: "↓",
  escape: "Esc",
  esc: "Esc",
  delete: "Del",
  backspace: "⌫",
  enter: "↵",
  space: "Space",
  plus: "+",
};

/** Human-readable parts of a shortcut, e.g. "mod+shift+z" → ["Ctrl", "Shift", "Z"]. */
export function shortcutParts(shortcut: string, mac: boolean = isMac): string[] {
  return shortcut.split("+").map((p) => {
    const k = p.toLowerCase();
    if (k === "mod") return mac ? "⌘" : "Ctrl";
    if (k === "shift") return mac ? "⇧" : "Shift";
    if (k === "alt") return mac ? "⌥" : "Alt";
    if (k === "ctrl") return mac ? "⌃" : "Ctrl";
    return KEY_LABELS[k] ?? (p.length === 1 ? p.toUpperCase() : p.charAt(0).toUpperCase() + p.slice(1));
  });
}

export function formatShortcut(shortcut: string, mac: boolean = isMac): string {
  return shortcutParts(shortcut, mac).join(mac ? "" : "+");
}
