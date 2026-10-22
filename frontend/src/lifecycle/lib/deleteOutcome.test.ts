import { describe, expect, it } from "vitest";

import { deleteToast, deleteWentLive, ifMatch } from "./deleteOutcome";

const t = (key: string) => key;

describe("delete outcome", () => {
  it("closes the DAG only when it left the target", () => {
    expect(deleteWentLive({ deleted: true, status: "deleted" })).toBe(true);
    expect(deleteWentLive({ deleted: false, status: "not_found" })).toBe(true);
    expect(deleteWentLive({ deleted: true })).toBe(true); // older servers send no status
    expect(deleteWentLive(undefined)).toBe(true);
    expect(deleteWentLive({ deleted: false, status: "pending_approval" })).toBe(false);
    expect(deleteWentLive({ deleted: true, status: "pr_opened" })).toBe(false);
  });

  it("reports proposals and pull requests", () => {
    expect(deleteToast({ deleted: false, status: "pending_approval" }, "d", t).title).toBe("toolbar.deletePending");
    expect(deleteToast({ deleted: true, status: "pr_opened" }, "d", t).title).toBe("toolbar.deletePrOpened");
    expect(deleteToast({ deleted: true }, "d", t).title).toBe("toolbar.deleted");
  });

  it("sends the loaded etag as If-Match", () => {
    expect(ifMatch("abc")).toEqual({ "if-match": '"abc"' });
    expect(ifMatch(null)).toEqual({});
    expect(ifMatch(undefined)).toEqual({});
    expect(ifMatch("")).toEqual({});
  });
});
