// @vitest-environment jsdom
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { metaFixture, renderWithProviders } from "../../test/render";
import DeployDialog from "./DeployDialog";

type Deploy = NonNullable<NonNullable<ReturnType<typeof metaFixture>["iam"]>["deploy"]>;

function gitMeta(details: Record<string, unknown>, deploy: Partial<Deploy> = {}) {
  return metaFixture({
    storage: { kind: "git", location: "git@example.com:org/dags.git", writable: true, details },
    iam: { deploy: { ...(metaFixture({ iam: {} }).iam?.deploy as Deploy), ...deploy } },
  });
}

const effect = () => screen.getByTestId("deploy-effect").textContent ?? "";

describe("DeployDialog effect", () => {
  it("says where a local deploy writes", () => {
    renderWithProviders(<DeployDialog focus="message" />);
    expect(effect()).toContain("Will write to /dags/autoair");
  });

  it("explains a direct push with the target folder", () => {
    renderWithProviders(<DeployDialog focus="message" />, {
      meta: gitMeta({ base_branch: "release", subdir: "dags/autoair", mode: "push" }, { git_mode: "push" }),
    });
    expect(effect()).toContain("Will push to release (dags/autoair/)");
  });

  it("announces a pull request and the approval rule", () => {
    renderWithProviders(<DeployDialog focus="message" />, {
      meta: gitMeta({ mode: "pr" }, { git_mode: "pr", default_opens_pr: true, require_approval: true }),
    });
    expect(effect()).toContain("Will open a pull request against main");
    expect(effect()).toContain("Going live needs an approval");
    expect(screen.queryByTestId("direct-push")).toBeNull();
  });

  it("switches from pull request to push when a privileged user skips the PR", async () => {
    renderWithProviders(<DeployDialog focus="message" />, {
      meta: gitMeta({ base_branch: "main", mode: "pr" }, { git_mode: "pr", default_opens_pr: true, direct_push: true }),
    });
    expect(effect()).toContain("pull request");
    const label = screen.getByText("Push directly (skip the pull request)").closest("label");
    if (!label) throw new Error("direct push checkbox not found");
    fireEvent.click(label);
    await waitFor(() => expect(effect()).toContain("Will push to main"));
  });
});
