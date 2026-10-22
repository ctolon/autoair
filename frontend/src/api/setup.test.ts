// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";

import { adoptFragmentToken, authHeaders, errorMessage } from "./setup";

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

describe("adoptFragmentToken", () => {
  afterEach(() => {
    sessionStorage.clear();
    window.history.replaceState(null, "", "/");
  });

  it("keeps the per-run token of autoair local and removes it from the address bar", () => {
    window.history.replaceState(null, "", "/ui/?dag=x#token=autoair-local-abc&dag=y");
    adoptFragmentToken();
    expect(authHeaders().Authorization).toBe("Bearer autoair-local-abc");
    expect(window.location.hash).toBe("#dag=y");
    expect(window.location.search).toBe("?dag=x");
  });

  it("ignores tokens of another shape but still drops them from the URL", () => {
    window.history.replaceState(null, "", "/ui/#token=eyJhbGciOi.attacker.sig");
    adoptFragmentToken();
    expect(authHeaders().Authorization).toBeUndefined();
    expect(window.location.hash).toBe("");
  });
});
