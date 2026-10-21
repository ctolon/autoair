import { describe, expect, it } from "vitest";
import { LuBox } from "react-icons/lu";

import type { InsertItem } from "./insertables";
import { matchScore, rankItems, searchTerms } from "./search";

const item = (label: string, hint = "", provider = "standard"): InsertItem => ({
  key: label,
  label,
  hint,
  provider,
  category: "operator",
  icon: LuBox,
  keywords: [],
  payload: () => ({ kind: "fragment", templateId: label }),
});

describe("insertable search", () => {
  const items = [
    item("S3ToSqlOperator", "Load S3 into SQL", "amazon"),
    item("SQLExecuteQueryOperator", "Run SQL", "common-sql"),
    item("JiraOperator", "Jira API", "atlassian-jira"),
    item("BigQueryInsertJobOperator", "Run a SQL job", "google"),
  ];

  it("requires every word and ranks label prefixes first", () => {
    expect(rankItems(items, "sql").map((i) => i.label)).toEqual([
      "SQLExecuteQueryOperator",
      "S3ToSqlOperator",
      "BigQueryInsertJobOperator",
    ]);
    expect(rankItems(items, "sql google").map((i) => i.label)).toEqual(["BigQueryInsertJobOperator"]);
    expect(rankItems(items, "jira").map((i) => i.label)).toEqual(["JiraOperator"]);
    expect(rankItems(items, "nothing-matches")).toEqual([]);
  });

  it("limits results and keeps everything for an empty query", () => {
    expect(rankItems(items, "operator", 2)).toHaveLength(2);
    expect(rankItems(items, "  ")).toHaveLength(4);
    expect(searchTerms("  Sql   JOB ")).toEqual(["sql", "job"]);
    expect(matchScore(item("X"), [])).toBe(1);
  });
});
