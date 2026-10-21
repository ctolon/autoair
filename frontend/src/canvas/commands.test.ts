import { describe, expect, it } from "vitest";

import {
  type Command,
  type KeyLike,
  findCommandForEvent,
  formatShortcut,
  matchesShortcut,
  registerCommands,
  runCommand,
} from "../lib/commands";

const key = (k: string, mods: Partial<KeyLike> = {}): KeyLike => ({
  key: k,
  ctrlKey: false,
  metaKey: false,
  shiftKey: false,
  altKey: false,
  ...mods,
});

describe("matchesShortcut", () => {
  it("maps mod to Ctrl (Linux/Windows) and ⌘ (macOS)", () => {
    expect(matchesShortcut(key("k", { ctrlKey: true }), "mod+k", false)).toBe(true);
    expect(matchesShortcut(key("k", { metaKey: true }), "mod+k", true)).toBe(true);
    expect(matchesShortcut(key("k", { ctrlKey: true }), "mod+k", true)).toBe(false);
    expect(matchesShortcut(key("k"), "mod+k", false)).toBe(false);
  });

  it("requires exact modifiers for letters", () => {
    expect(matchesShortcut(key("Z", { ctrlKey: true, shiftKey: true }), "mod+shift+z", false)).toBe(true);
    expect(matchesShortcut(key("z", { ctrlKey: true, shiftKey: true }), "mod+z", false)).toBe(false);
    expect(matchesShortcut(key("g", { ctrlKey: true, altKey: true }), "mod+g", false)).toBe(false);
  });

  it("implies Shift for symbols and understands key names", () => {
    expect(matchesShortcut(key("?", { shiftKey: true }), "?", false)).toBe(true);
    expect(matchesShortcut(key("Escape"), "esc", false)).toBe(true);
    expect(matchesShortcut(key("ArrowLeft", { shiftKey: true }), "shift+arrowleft", false)).toBe(true);
    expect(matchesShortcut(key("ArrowLeft"), "shift+arrowleft", false)).toBe(false);
    expect(matchesShortcut(key("Delete"), "delete", false)).toBe(true);
  });
});

describe("findCommandForEvent", () => {
  const run = () => undefined;
  const list: Command[] = [
    { id: "native", title: "copy", shortcut: "mod+c", handledNatively: true, run },
    { id: "off", title: "off", shortcut: "mod+d", when: () => false, run },
    { id: "dup", title: "dup", shortcut: "mod+d", run },
    { id: "del", title: "del", shortcut: "delete", altShortcuts: ["backspace"], run },
    { id: "nudge", title: "nudge", shortcut: "arrowleft", repeat: true, run },
  ];

  it("skips native, disabled and non-repeatable commands", () => {
    expect(findCommandForEvent(key("c", { ctrlKey: true }), list, false)).toBeUndefined();
    expect(findCommandForEvent(key("d", { ctrlKey: true }), list, false)?.id).toBe("dup");
    expect(findCommandForEvent(key("Backspace"), list, false)?.id).toBe("del");
    expect(findCommandForEvent({ ...key("Delete"), repeat: true }, list, false)).toBeUndefined();
    expect(findCommandForEvent({ ...key("ArrowLeft"), repeat: true }, list, false)?.id).toBe("nudge");
  });
});

describe("registry", () => {
  it("runs enabled commands and unregisters", () => {
    let n = 0;
    const off = registerCommands({ id: "t.inc", title: "inc", run: () => void n++ });
    runCommand("t.inc");
    off();
    runCommand("t.inc");
    expect(n).toBe(1);
  });

  it("formats shortcuts per platform", () => {
    expect(formatShortcut("mod+shift+z", false)).toBe("Ctrl+Shift+Z");
    expect(formatShortcut("mod+shift+z", true)).toBe("⌘⇧Z");
    expect(formatShortcut("shift+arrowleft", false)).toBe("Shift+←");
  });
});
