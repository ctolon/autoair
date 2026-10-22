// @vitest-environment jsdom
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const sdk = vi.hoisted(() => ({ getToolCallArguments: vi.fn() }));
vi.mock("../api/gen/sdk.gen", () => sdk);

import type { ChatMessage, ToolApprovalView } from "../api/gen/types.gen";
import en from "../i18n/locales/en/ai.json";
import tr from "../i18n/locales/tr/ai.json";
import { renderWithProviders } from "../test/render";
import { awaitingApproval, mergeTurn } from "./store";
import { ToolApprovalCard, decisionsFor, formatArguments, isOpen, shownStatus } from "./ToolApprovalCard";

const FUTURE = new Date(Date.now() + 3_600_000).toISOString();

function approval(overrides: Partial<ToolApprovalView> = {}): ToolApprovalView {
  return {
    id: "ap1",
    agent: "developer",
    status: "pending",
    created_at: new Date().toISOString(),
    expires_at: FUTURE,
    calls: [
      {
        id: "t1",
        tool: "remember",
        kind: "write",
        description: "Store a convention.",
        arguments: { text: "Use pools", password: "***" },
      },
    ],
    ...overrides,
  };
}

describe("approval helpers", () => {
  it("formats any argument value without throwing", () => {
    expect(formatArguments({ a: 1 })).toContain('"a": 1');
    expect(formatArguments("plain")).toBe("plain");
    expect(formatArguments(null)).toBe("");
    expect(formatArguments(undefined)).toBe("");
    expect(formatArguments(42)).toBe("42");
    const cyclic: Record<string, unknown> = {};
    cyclic.self = cyclic;
    expect(formatArguments(cyclic)).toBe("[object Object]");
    expect(formatArguments(10n)).toBe("10");
    expect(formatArguments("x".repeat(50_000)).length).toBe(20_000);
  });

  it("needs a decision for every call", () => {
    const calls = approval().calls;
    expect(decisionsFor(calls, {})).toBeNull();
    expect(decisionsFor(undefined, {})).toBeNull();
    expect(decisionsFor(calls, { t1: { approved: true, reason: "ignored" } })).toEqual([{ id: "t1", approved: true }]);
    expect(decisionsFor(calls, { t1: { approved: false, reason: "  not now " } })).toEqual([
      { id: "t1", approved: false, reason: "not now" },
    ]);
    expect(decisionsFor(calls, { t1: { approved: false, reason: "   " } })).toEqual([{ id: "t1", approved: false }]);
  });

  it("shows a pending approval past its expiry as expired", () => {
    expect(shownStatus(approval())).toBe("pending");
    expect(isOpen(approval({ expires_at: "2000-01-01T00:00:00Z" }))).toBe(false);
    expect(shownStatus(approval({ expires_at: "not a date" }))).toBe("pending");
    expect(isOpen(approval({ status: "resolved" }))).toBe(false);
    expect(isOpen(null)).toBe(false);
  });

  it("has every status and kind in English and Turkish", () => {
    for (const status of ["pending", "resuming", "resolved", "expired", "cancelled", "failed"] as const) {
      expect(en.approval.status[status]).toBeTruthy();
      expect(tr.approval.status[status]).toBeTruthy();
    }
    expect(tr.approval.kind.write).toBeTruthy();
    expect(en.troubleshoot.status.awaiting_approval).toBeTruthy();
    expect(tr.troubleshoot.status.awaiting_approval).toBeTruthy();
  });
});

const DIGEST = "a".repeat(64);

describe("ToolApprovalCard", () => {
  beforeEach(() => sdk.getToolCallArguments.mockReset());

  it("binds an approval to the digest of the arguments shown", () => {
    const calls = approval({ calls: [{ id: "t1", tool: "run", kind: "execute", arguments_sha256: DIGEST }] }).calls;
    expect(decisionsFor(calls, { t1: { approved: true, reason: "" } })).toEqual([
      { id: "t1", approved: true, arguments_sha256: DIGEST },
    ]);
    // a denial runs nothing: no digest needed
    expect(decisionsFor(calls, { t1: { approved: false, reason: "" } })).toEqual([{ id: "t1", approved: false }]);
    expect(en.approval.kind.execute).toBeTruthy();
    expect(tr.approval.kind.execute).toBeTruthy();
  });

  it("marks cut arguments and loads the full ones before approving", async () => {
    sdk.getToolCallArguments.mockResolvedValue({
      data: {
        id: "t1",
        tool: "import_python_dag",
        arguments: { source: "x".repeat(900) + "TAIL" },
        arguments_sha256: DIGEST,
      },
    });
    const cut = approval({
      calls: [
        {
          id: "t1",
          tool: "import_python_dag",
          kind: "execute",
          arguments: { source: "x".repeat(100) + "…" },
          truncated: true,
          omitted_chars: 804,
          arguments_sha256: DIGEST,
        },
      ],
    });
    renderWithProviders(<ToolApprovalCard approval={cut} onSubmit={vi.fn()} />);
    const marker = screen.getByTestId("tool-approval-truncated");
    expect(marker.textContent).toContain("804");
    expect(screen.getByTestId("tool-approval-arguments").textContent).not.toContain("TAIL");
    fireEvent.click(screen.getByTestId("tool-approval-show-full"));
    await waitFor(() => expect(screen.getByTestId("tool-approval-arguments").textContent).toContain("TAIL"));
    expect(sdk.getToolCallArguments).toHaveBeenCalledWith(
      expect.objectContaining({ path: { approval_id: "ap1", call_id: "t1" } }),
    );
    expect(screen.queryByTestId("tool-approval-mismatch")).toBeNull();
    fireEvent.click(screen.getByTestId("tool-approval-show-short"));
    expect(screen.getByTestId("tool-approval-arguments").textContent).not.toContain("TAIL");
  });

  it("warns when the full arguments do not match the approval, and survives a failed load", async () => {
    sdk.getToolCallArguments.mockResolvedValueOnce({
      data: { id: "t1", tool: "t", arguments: {}, arguments_sha256: "b".repeat(64) },
    });
    const cut = approval({
      calls: [{ id: "t1", tool: "t", arguments: {}, truncated: true, arguments_sha256: DIGEST }],
    });
    const view = renderWithProviders(<ToolApprovalCard approval={cut} onSubmit={vi.fn()} />);
    fireEvent.click(screen.getByTestId("tool-approval-show-full"));
    await waitFor(() => expect(screen.getByTestId("tool-approval-mismatch")).toBeTruthy());
    view.unmount();
    sdk.getToolCallArguments.mockRejectedValueOnce(new Error("404"));
    renderWithProviders(<ToolApprovalCard approval={cut} onSubmit={vi.fn()} />);
    fireEvent.click(screen.getByTestId("tool-approval-show-full"));
    await waitFor(() => expect(screen.getByText(en.approval.fullFailed)).toBeTruthy());
    expect(screen.getByTestId("tool-approval-card")).toBeTruthy();
  });

  it("sends one decision per call after the user chose", () => {
    const onSubmit = vi.fn();
    renderWithProviders(<ToolApprovalCard approval={approval()} onSubmit={onSubmit} />);
    const card = screen.getByTestId("tool-approval-card");
    expect(within(card).getByText("remember")).toBeTruthy();
    expect(screen.getByTestId("tool-approval-arguments").textContent).toContain('"password": "***"');
    const submit = screen.getByTestId("tool-approval-submit") as HTMLButtonElement;
    expect(submit.disabled).toBe(true);
    fireEvent.click(screen.getByTestId("tool-approval-approve"));
    expect(submit.disabled).toBe(false);
    fireEvent.click(submit);
    expect(onSubmit).toHaveBeenCalledWith([{ id: "t1", approved: true }]);
  });

  it("sends a denial with its reason", () => {
    const onSubmit = vi.fn();
    renderWithProviders(<ToolApprovalCard approval={approval()} onSubmit={onSubmit} />);
    fireEvent.click(screen.getByTestId("tool-approval-deny"));
    fireEvent.change(screen.getByTestId("tool-approval-reason"), { target: { value: "not now" } });
    fireEvent.click(screen.getByTestId("tool-approval-submit"));
    expect(onSubmit).toHaveBeenCalledWith([{ id: "t1", approved: false, reason: "not now" }]);
  });

  it("decides several calls at once", () => {
    const onSubmit = vi.fn();
    const two = approval({
      calls: [
        { id: "a", tool: "x", arguments: {} },
        { id: "b", tool: "y", arguments: {} },
      ],
    });
    renderWithProviders(<ToolApprovalCard approval={two} onSubmit={onSubmit} />);
    fireEvent.click(screen.getByRole("button", { name: en.approval.denyAll }));
    fireEvent.click(screen.getByTestId("tool-approval-submit"));
    expect(onSubmit).toHaveBeenCalledWith([
      { id: "a", approved: false },
      { id: "b", approved: false },
    ]);
  });

  it("shows the outcome of a decided approval without buttons", () => {
    renderWithProviders(
      <ToolApprovalCard
        approval={approval({
          status: "resolved",
          decided_by: "alice",
          decisions: [{ id: "t1", approved: false, reason: "not now" }],
        })}
        onSubmit={vi.fn()}
      />,
    );
    expect(screen.getByTestId("tool-approval-status").textContent).toBe(en.approval.status.resolved);
    expect(screen.getByTestId("tool-approval-decision").textContent).toContain(en.approval.denied);
    expect(screen.queryByTestId("tool-approval-submit")).toBeNull();
    expect(screen.getByText(/alice/)).toBeTruthy();
  });

  it("never breaks on malformed approvals", () => {
    const malformed = [
      { id: "m1", agent: "developer", created_at: "", expires_at: "" },
      { ...approval(), status: "mystery" },
      { ...approval(), calls: null },
      { ...approval(), calls: [null, { id: 3 }, { id: "ok", tool: null, arguments: undefined, kind: "weird" }] },
      { ...approval(), decisions: "nope", calls: [{ id: "t1", tool: "t", arguments: [1, [2, [3]]] }] },
      { ...approval(), status: "failed", error: null },
    ] as unknown as ToolApprovalView[];
    for (const item of malformed) {
      const view = renderWithProviders(<ToolApprovalCard approval={item} onSubmit={vi.fn()} />);
      expect(view.getByTestId("tool-approval-card")).toBeTruthy();
      view.unmount();
    }
  });

  it("is read-only without a submit handler", () => {
    renderWithProviders(<ToolApprovalCard approval={approval()} />);
    expect(screen.queryByTestId("tool-approval-approve")).toBeNull();
  });
});

describe("chat store with approvals", () => {
  const msg = (role: "user" | "assistant", content: string, id = content): ChatMessage => ({ id, role, content });

  it("completes the paused answer in place when a run resumes", () => {
    const paused: ChatMessage = { ...msg("assistant", "Storing.", "m2"), approvals: [approval()] };
    const out = mergeTurn(
      [msg("user", "a", "m1"), paused],
      {
        thread: { id: "t", mode: "developer", title: "x", created_at: "", updated_at: "" },
        user_message: msg("user", "a", "m1"),
        message: { ...msg("assistant", "Storing.\n\nDone.", "m2"), approvals: [approval({ status: "resolved" })] },
      },
      false,
      "m2",
    );
    expect(out.map((m) => m.content)).toEqual(["a", "Storing.\n\nDone."]);
    expect(awaitingApproval([msg("user", "a"), paused])).toBe(true);
    expect(awaitingApproval(out)).toBe(false);
    expect(awaitingApproval([{ ...paused, approvals: "bad" as unknown as [] }])).toBe(false);
  });
});
