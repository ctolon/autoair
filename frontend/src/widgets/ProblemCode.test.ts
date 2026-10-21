import { describe, expect, it } from "vitest";

import { parseProblemCode, splitLine } from "./ProblemCode";

describe("problem codes", () => {
  it("links lint rules to their docs", () => {
    expect(parseProblemCode("lint:B018")).toMatchObject({
      tool: "ruff",
      rule: "B018",
      url: null, // the server sends ruff's real rule page as doc_url
    });
    expect(parseProblemCode("SC2086").url).toBe("https://www.shellcheck.net/wiki/SC2086");
    expect(parseProblemCode("param")).toMatchObject({ tool: null, rule: "param", url: null });
  });
  it("moves the line prefix out of the message", () => {
    expect(splitLine("line 2: Found useless expression")).toEqual([2, "Found useless expression"]);
    expect(splitLine("required parameter", 4)).toEqual([4, "required parameter"]);
    expect(splitLine("required parameter")).toEqual([null, "required parameter"]);
  });
});
