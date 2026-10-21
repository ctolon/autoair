import { describe, expect, it } from "vitest";

import { declaredVariables } from "./bashLsp";

describe("declaredVariables", () => {
  it("finds assignments, exports, loops and reads", () => {
    const script =
      'set -e\nOUT=/tmp/x\nexport BUCKET="b"\nfor f in *.csv; do echo "$f"; done\nread -r NAME\nlocal COUNT=0';
    expect(declaredVariables(script).sort()).toEqual(["BUCKET", "COUNT", "NAME", "OUT", "f"]);
  });
  it("ignores comparisons and options", () => {
    expect(declaredVariables("if [ $a == b ]; then echo; fi\ncurl --data=x")).toEqual([]);
  });
});
