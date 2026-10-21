import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

/**
 * Store selectors must return stable values. `useShallow` compares array items / object values
 * by identity, so a selector that maps to *new objects* (`.map((p) => ({ … }))`) changes on
 * every read and React aborts with "Maximum update depth exceeded" — this broke the Python
 * editor as soon as lint reported a problem. Selectors without `useShallow` must not build new
 * arrays or objects at all. Derive such data with `useMemo` outside the selector instead.
 */
/** Every zustand store hook in the app (`export const useX = create…`), found from the source. */
function storeHooks(files: string[]): string[] {
  const names = new Set<string>();
  for (const file of files) {
    for (const m of readFileSync(file, "utf8").matchAll(/export const (use[A-Z]\w*)\s*=\s*create\b/g)) {
      if (m[1]) names.add(m[1]);
    }
  }
  return [...names];
}
const BUILDS_COLLECTION = /\.(map|filter|slice|concat|flatMap)\(|Object\.(keys|values|entries)\(|=>\s*\(\{|=>\s*\[/;
// New objects or arrays per item: `.map((p) => ({…}))` / `.map((p) => […])`.
const MAPS_TO_OBJECTS = /\.map\(\s*\(?[\w, ]*\)?\s*=>\s*[({[]\s*[{[]?/;

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return name === "gen" ? [] : sources(path);
    return /\.tsx?$/.test(name) && !name.endsWith(".test.ts") ? [path] : [];
  });
}

/** Text of the selector argument: from the arrow to the hook call's closing parenthesis. */
function selectorBody(src: string, callStart: number, bodyStart: number): string {
  let depth = 0;
  for (let i = src.indexOf("(", callStart); i < src.length; i++) {
    if (src[i] === "(") depth++;
    else if (src[i] === ")" && --depth === 0) return src.slice(bodyStart, i);
  }
  return src.slice(bodyStart);
}

describe("store selectors", () => {
  it("return stable values", () => {
    const offenders: string[] = [];
    const files = sources(join(__dirname, ".."));
    const hooks = storeHooks(files);
    expect(hooks).toEqual(expect.arrayContaining(["useBuilder", "useLifecycle", "useLayout", "useFocusRequest"]));
    // useX(selector) / useX(useShallow(selector)) with any parameter name.
    const STORE_HOOK = new RegExp(
      `\\b(?:${hooks.join("|")})\\(\\s*(useShallow\\()?\\s*\\(?\\s*[A-Za-z_$][\\w$]*\\s*\\)?\\s*=>`,
      "g",
    );
    for (const file of files) {
      const src = readFileSync(file, "utf8");
      for (const m of src.matchAll(STORE_HOOK)) {
        const body = selectorBody(src, m.index, m.index + m[0].length);
        const shallow = !!m[1];
        const unstable = shallow
          ? MAPS_TO_OBJECTS.test(body)
          : BUILDS_COLLECTION.test(body) && !/\.length\b/.test(body);
        if (unstable) offenders.push(`${file}:${src.slice(0, m.index).split("\n").length}`);
      }
    }
    expect(offenders).toEqual([]);
  });
});
