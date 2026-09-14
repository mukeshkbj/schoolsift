import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import SchoolSiftApp from "../components/SchoolSiftApp";
import {
  bootstrapAgentMissing,
  bootstrapAwaitingAgent,
  bootstrapProcessed,
  bootstrapWithFailedMessage,
  packet,
} from "./helpers";

vi.mock("../lib/api", async () => {
  const { child, household } = await import("./helpers");
  return {
    getBootstrap: vi.fn(),
    createHousehold: vi.fn().mockResolvedValue(household),
    addChild: vi.fn().mockResolvedValue(child),
    startConnect: vi.fn(),
    disconnectConnection: vi.fn(),
    syncConnection: vi.fn(),
    enableNotifications: vi.fn(),
    confirmSource: vi.fn(),
    rejectSource: vi.fn(),
    processMessage: vi.fn(),
    editProposal: vi.fn(),
    approveProposal: vi.fn(),
    rejectProposal: vi.fn(),
  };
});

const api = () => import("../lib/api");

describe("SchoolSiftApp message processing", () => {
  it("processes an awaiting message and shows the new packet", async () => {
    const { getBootstrap, processMessage } = await api();
    vi.mocked(getBootstrap).mockResolvedValue(bootstrapProcessed);
    vi.mocked(processMessage).mockResolvedValue(packet);
    render(<SchoolSiftApp initialBootstrap={bootstrapAwaitingAgent} />);
    await userEvent.click(
      screen.getByRole("button", { name: "Process message" }),
    );
    await waitFor(() =>
      expect(processMessage).toHaveBeenCalledWith("msg-2"),
    );
    await waitFor(() => expect(getBootstrap).toHaveBeenCalled());
    expect(await screen.findByText(packet.summary)).toBeInTheDocument();
  });

  it("shows agent configuration guidance when the agent is unavailable", () => {
    render(<SchoolSiftApp initialBootstrap={bootstrapAgentMissing} />);
    expect(
      screen.getByText(/Waiting for agent configuration/),
    ).toBeInTheDocument();
    expect(screen.getByText("SCHOOLSIFT_AWS_REGION")).toBeInTheDocument();
    expect(screen.getByText("SCHOOLSIFT_BEDROCK_MODEL_ID")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Process message" }),
    ).not.toBeInTheDocument();
  });

  it("shows the manual review reason for failed messages", () => {
    render(<SchoolSiftApp initialBootstrap={bootstrapWithFailedMessage} />);
    expect(
      screen.getByText(/exceeds the 25 MB limit/),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Process message" }),
    ).not.toBeInTheDocument();
  });
});
