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
      screen.getByRole("button", { name: "Send demo reply" }),
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
    expect(screen.getByRole("button", { name: "Send demo reply" })).toBeDisabled();
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
    const approve = screen.getByRole("button", { name: "Send demo reply" });
    expect(approve).toBeDisabled();
    expect(approve).toHaveAttribute("aria-busy", "true");
    expect(approve).toHaveTextContent("Send demo reply");
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

  it("shows completed state", () => {
    render(
      <ProposalReview
        {...baseProps}
        version={{ ...replyVersion, status: "completed" }}
      />,
    );
    expect(screen.getByText(/recorded in the demo outbox/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Send demo reply" })).toBeNull();
  });
});
