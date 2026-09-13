import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import DemoWorkspace from "../components/DemoWorkspace";
import { demoState } from "./helpers";

vi.mock("../lib/api", async (importOriginal) => {
  const mod = await importOriginal<typeof import("../lib/api")>();
  const { demoState: state } = await import("./helpers");
  return {
    ...mod,
    getDemo: vi.fn().mockResolvedValue(state),
    processInbox: vi.fn().mockResolvedValue({ packets: state.packets }),
    approveProposal: vi
      .fn()
      .mockResolvedValue({ version: {}, outcome: { kind: "demo_reply" } }),
  };
});

describe("DemoWorkspace", () => {
  it("shows the Demo data label and one primary process action", () => {
    render(<DemoWorkspace initialState={{ ...demoState, packets: [] }} />);
    expect(screen.getByText("Demo data")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Process demo inbox" }),
    ).toBeInTheDocument();
  });

  it("keeps the process label stable and marks the button busy", async () => {
    const api = await import("../lib/api");
    vi.mocked(api.processInbox).mockReturnValueOnce(new Promise(() => {}));
    render(<DemoWorkspace initialState={{ ...demoState, packets: [] }} />);
    await userEvent.click(
      screen.getByRole("button", { name: "Process demo inbox" }),
    );
    const btn = screen.getByRole("button", { name: "Process demo inbox" });
    expect(btn).toHaveTextContent("Process demo inbox");
    expect(btn).toHaveAttribute("aria-busy", "true");
    expect(btn).toBeDisabled();
  });

  it("shows the empty state before processing", () => {
    render(<DemoWorkspace initialState={{ ...demoState, packets: [] }} />);
    expect(screen.getByText(/Six synthetic school messages/)).toBeInTheDocument();
  });

  it("lists inbox messages and selects a packet", async () => {
    render(<DemoWorkspace initialState={demoState} />);
    await screen.findByRole("button", { name: /field trip/i });
    await userEvent.click(
      screen.getByRole("button", { name: /field trip/i }),
    );
    expect(screen.getByText("For Maya")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Send demo reply" }),
    ).toBeInTheDocument();
  });
});
