import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import ProposalReview from "../components/ProposalReview";
import { escalationVersion, replyVersion } from "./helpers";

const baseProps = {
  busyKey: null,
  conflict: null,
  onSave: vi.fn().mockResolvedValue(undefined),
  onApprove: vi.fn().mockResolvedValue(undefined),
  onReject: vi.fn().mockResolvedValue(undefined),
  onReload: vi.fn().mockResolvedValue(undefined),
};

describe("ProposalReview", () => {
  it("names the approve button with its consequence", () => {
    render(<ProposalReview {...baseProps} version={replyVersion} />);
    expect(
      screen.getByRole("button", { name: "Approve reply" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /confirm|submit/i })).toBeNull();
  });

  it("enables saving only after an edit", async () => {
    render(<ProposalReview {...baseProps} version={replyVersion} />);
    const save = screen.getByRole("button", { name: "Save edit as new version" });
    expect(save).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Body"), " Edited.");
    expect(save).toBeEnabled();
  });

  it("disables approve while edits are unsaved and says why", async () => {
    render(<ProposalReview {...baseProps} version={replyVersion} />);
    await userEvent.type(screen.getByLabelText("Body"), " Edited.");
    expect(screen.getByRole("button", { name: "Approve reply" })).toBeDisabled();
    expect(screen.getByText("Save your edits before sending.")).toBeVisible();
  });

  it("keeps the approve label stable while busy", () => {
    render(
      <ProposalReview
        {...baseProps}
        version={replyVersion}
        busyKey={`${replyVersion.id}:approve`}
      />,
    );
    const approve = screen.getByRole("button", { name: "Approve reply" });
    expect(approve).toBeDisabled();
    expect(approve).toHaveAttribute("aria-busy", "true");
    expect(approve).toHaveTextContent("Approve reply");
  });

  it("never offers approval or save controls for escalations", () => {
    render(<ProposalReview {...baseProps} version={escalationVersion} />);
    expect(screen.queryByRole("button", { name: /send|add|complete/i })).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Save edit as new version" }),
    ).toBeNull();
    expect(screen.getByText(/Approval unavailable/)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Reject proposal" }),
    ).toBeInTheDocument();
  });

  it("shows a stale-conflict notice with a reload path", () => {
    render(
      <ProposalReview
        {...baseProps}
        version={replyVersion}
        conflict="Another decision already won."
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("Another decision");
    expect(screen.getByRole("button", { name: "Reload latest" })).toBeInTheDocument();
  });

  it("shows the approved state", () => {
    render(
      <ProposalReview
        {...baseProps}
        version={{ ...replyVersion, status: "approved" }}
      />,
    );
    expect(screen.getByText(/queued for delivery/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve reply" })).toBeNull();
  });
});

describe("ProposalReview execution", () => {
  const approvedReply = { ...replyVersion, status: "approved" as const };

  const execution = (status: string, safe_error: string | null = null) => ({
    id: "exec-1",
    household_id: "hh-1",
    proposal_id: replyVersion.id,
    proposal_version: 1,
    connection_id: "conn-1",
    idempotency_key: "idem-1",
    status: status as
      | "pending_dispatch"
      | "queued"
      | "executing"
      | "completed"
      | "failed"
      | "delivery_uncertain",
    attempts: 1,
    provider_operation_id: null,
    safe_error,
    created_at: "2026-09-13T00:00:00Z",
    updated_at: "2026-09-13T00:00:00Z",
  });

  it("states that approving sends the email", () => {
    render(<ProposalReview {...baseProps} version={replyVersion} />);
    expect(
      screen.getByText(/Approving will send this email to/),
    ).toBeInTheDocument();
  });

  it("in local mode states that approving only queues the email", () => {
    render(
      <ProposalReview {...baseProps} version={replyVersion} localMode />,
    );
    expect(
      screen.getByText(
        /Approving queues this email to .*nothing is sent until you run the approved action/,
      ),
    ).toBeInTheDocument();
  });

  it("shows each execution status distinctly", () => {
    const labels: Record<string, RegExp> = {
      pending_dispatch: /Pending — approved but not yet dispatched/,
      queued: /Queued — ready to run/,
      executing: /Executing against the connected account/,
      completed: /Completed — the provider accepted/,
      failed: /Failed — the action did not complete/,
      delivery_uncertain: /Delivery uncertain/,
    };
    for (const [status, label] of Object.entries(labels)) {
      const { unmount } = render(
        <ProposalReview
          {...baseProps}
          version={approvedReply}
          execution={execution(status)}
        />,
      );
      expect(screen.getByText(label)).toBeInTheDocument();
      unmount();
    }
  });

  it("surfaces safe_error for failed executions", () => {
    render(
      <ProposalReview
        {...baseProps}
        version={approvedReply}
        execution={execution("failed", "Provider rejected the request.")}
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Provider rejected the request.",
    );
  });

  it("local mode offers dispatch for pending executions", async () => {
    const onDispatch = vi.fn().mockResolvedValue(undefined);
    render(
      <ProposalReview
        {...baseProps}
        version={approvedReply}
        execution={execution("pending_dispatch")}
        localMode
        onDispatch={onDispatch}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Dispatch" }));
    expect(onDispatch).toHaveBeenCalledOnce();
  });

  it("run requires the confirmation checkbox and names the consequence", async () => {
    const onRun = vi.fn().mockResolvedValue(undefined);
    render(
      <ProposalReview
        {...baseProps}
        version={approvedReply}
        execution={execution("queued")}
        localMode
        onRun={onRun}
      />,
    );
    const run = screen.getByRole("button", { name: "Run approved action" });
    expect(run).toBeDisabled();
    expect(
      screen.getByText(/send this email to office@maplegrove.example/),
    ).toBeInTheDocument();
    await userEvent.click(screen.getByRole("checkbox"));
    expect(run).toBeEnabled();
    await userEvent.click(run);
    expect(onRun).toHaveBeenCalledOnce();
  });

  it("hides dispatch and run for viewers and non-local mode", () => {
    render(
      <ProposalReview
        {...baseProps}
        version={approvedReply}
        execution={execution("queued")}
        readOnly
        localMode
        onRun={vi.fn()}
        onDispatch={vi.fn()}
      />,
    );
    expect(
      screen.queryByRole("button", { name: "Run approved action" }),
    ).toBeNull();
  });
});
