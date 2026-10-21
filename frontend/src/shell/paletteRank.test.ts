import { describe, expect, it } from "vitest";

import { paletteScore } from "./paletteRank";

type Entry = { value: string; keywords: string[] };

const entries: Entry[] = [
  { value: "cmd:dag.deploy", keywords: ["Deploy", "File", "save"] },
  { value: "cmd:dag.trigger", keywords: ["Trigger run…", "Run"] },
  { value: "cmd:shell.toggleColorMode", keywords: ["Toggle dark mode", "View", "theme"] },
  { value: "builtin:external_python", keywords: ["@task.external_python", "Runs in an existing Python interpreter"] },
  { value: "builtin:docker", keywords: ["@task.docker", "Runs inside a Docker container"] },
  {
    value: "op:TriggerDagRunOperator",
    keywords: ["TriggerDagRunOperator", "Triggers a DAG run for a specified DAG ID"],
  },
  { value: "op:S3KeySensor", keywords: ["S3KeySensor", "Waits for one or multiple keys in an S3 bucket"] },
];

function rank(search: string): string[] {
  return entries
    .map((e) => ({ v: e.value, s: paletteScore(e.value, search, e.keywords) }))
    .filter((x) => x.s > 0)
    .sort((a, b) => b.s - a.s)
    .map((x) => x.v);
}

describe("command palette ranking", () => {
  it("puts commands with a prefix match first", () => {
    expect(rank("dep")[0]).toBe("cmd:dag.deploy");
    expect(rank("trig")[0]).toBe("cmd:dag.trigger");
    expect(rank("trig")).toContain("op:TriggerDagRunOperator");
  });

  it("does not let fuzzy subsequences beat prefix / word-start matches", () => {
    // "dep" is a subsequence of nothing relevant: external_python must not match on title.
    expect(rank("dep")).not.toContain("builtin:external_python");
    expect(paletteScore("x", "s3k", ["S3KeySensor"])).toBeGreaterThan(paletteScore("x", "skr", ["S3KeySensor"]));
    // word start ("key" in S3KeySensor's camelCase) beats a hint substring
    expect(paletteScore("op:S3KeySensor", "key", ["S3KeySensor", "Waits for keys"])).toBeGreaterThan(
      paletteScore("op:Other", "key", ["Other", "Waits for keys"]),
    );
  });

  it("matches synonyms and secondary texts", () => {
    expect(rank("theme")[0]).toBe("cmd:shell.toggleColorMode");
    expect(rank("save")[0]).toBe("cmd:dag.deploy");
  });

  it("requires every term and accepts an empty query", () => {
    expect(rank("docker container")).toEqual(["builtin:docker"]);
    expect(rank("docker zzz")).toEqual([]);
    expect(paletteScore("anything", "  ")).toBe(1);
  });
});
