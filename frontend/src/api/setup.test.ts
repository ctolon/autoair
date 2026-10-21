import { describe, expect, it } from "vitest";

import { errorMessage } from "./setup";

describe("errorMessage", () => {
  it("shows the message of a structured detail (refused promotion)", () => {
    const err = {
      detail: {
        status: "invalid",
        message: "validation failed for environment 'prod'; nothing was written: x",
        report: {},
      },
    };
    expect(errorMessage(err)).toBe("validation failed for environment 'prod'; nothing was written: x");
  });

  it("keeps string and list details", () => {
    expect(errorMessage({ detail: "nope" })).toBe("nope");
    expect(errorMessage({ detail: [{ loc: ["body", "to_env"], msg: "required" }] })).toBe("to_env: required");
  });
});
