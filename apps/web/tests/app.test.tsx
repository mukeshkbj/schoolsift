import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import SchoolSiftApp from "../components/SchoolSiftApp";
import {
  bootstrapAwaitingAgent,
  bootstrapConnected,
  bootstrapEmpty,
  bootstrapHouseholdOnly,
  bootstrapReady,
  bootstrapWithPacket,
  bootstrapWithSource,
  inboxMessage,
  suggestedSource,
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
    reanalyzeMessage: vi.fn(),
    editProposal: vi.fn(),
    approveProposal: vi.fn(),
    rejectProposal: vi.fn(),
    listMembers: vi.fn(),
    listInvitations: vi.fn(),
    createInvitation: vi.fn(),
    revokeInvitation: vi.fn(),
    acceptInvitation: vi.fn(),
    changeMemberRole: vi.fn(),
    removeMember: vi.fn(),
  };
});

const api = () => import("../lib/api");

describe("SchoolSiftApp onboarding", () => {
  it("shows the household setup form when no household exists", () => {
    render(<SchoolSiftApp initialBootstrap={bootstrapEmpty} />);
    expect(screen.getByLabelText("Household name")).toBeInTheDocument();
    expect(screen.getByLabelText(/Time zone/)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Create household" }),
    ).toBeInTheDocument();
  });

  it("submits the household form", async () => {
    const { createHousehold, getBootstrap } = await api();
    vi.mocked(getBootstrap).mockResolvedValue(bootstrapHouseholdOnly);
    render(<SchoolSiftApp initialBootstrap={bootstrapEmpty} />);
    await userEvent.type(screen.getByLabelText("Household name"), "Rivera family");
    await userEvent.click(
      screen.getByRole("button", { name: "Create household" }),
    );
    await waitFor(() =>
      expect(createHousehold).toHaveBeenCalledWith(
        "Rivera family",
        expect.any(String),
      ),
    );
  });

  it("shows the add-child form for a household with no children", async () => {
    const { addChild, getBootstrap } = await api();
    vi.mocked(getBootstrap).mockResolvedValue(bootstrapReady);
    render(<SchoolSiftApp initialBootstrap={bootstrapHouseholdOnly} />);
    expect(screen.getByLabelText("Child name")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Child name"), "Maya");
    await userEvent.type(screen.getByLabelText("School"), "Maple Grove Elementary");
    await userEvent.type(screen.getByLabelText("Grade"), "3");
    await userEvent.click(screen.getByRole("button", { name: "Add child" }));
    await waitFor(() =>
      expect(addChild).toHaveBeenCalledWith(
        "Maya",
        "Maple Grove Elementary",
        "3",
      ),
    );
  });
});

describe("SchoolSiftApp connections", () => {
  it("names missing provider env vars and disables connect", async () => {
    render(<SchoolSiftApp initialBootstrap={bootstrapReady} />);
    await userEvent.click(
      screen.getByRole("button", { name: /^Accounts/ }),
    );
    expect(screen.getByText("GMAIL_CLIENT_ID")).toBeInTheDocument();
    expect(screen.getByText("GMAIL_CLIENT_SECRET")).toBeInTheDocument();
    expect(screen.getByText("OUTLOOK_CLIENT_ID")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Connect Gmail" }),
    ).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "Connect Outlook" }),
    ).toBeDisabled();
  });

  it("redirects to the provider authorization URL", async () => {
    const { startConnect } = await api();
    vi.mocked(startConnect).mockResolvedValue({
      authorization_url: "https://auth.example/authorize?state=xyz",
    });
    const onNavigate = vi.fn();
    render(
      <SchoolSiftApp
        initialBootstrap={bootstrapConnected}
        onNavigate={onNavigate}
      />,
    );
    await userEvent.click(
      screen.getByRole("button", { name: /^Accounts/ }),
    );
    await userEvent.click(screen.getByRole("button", { name: "Connect Gmail" }));
    await waitFor(() =>
      expect(onNavigate).toHaveBeenCalledWith(
        "https://auth.example/authorize?state=xyz",
      ),
    );
  });

  it("shows the connected status and strips the query param", async () => {
    window.history.replaceState(null, "", "/app?connected=gmail");
    const replaceSpy = vi.spyOn(window.history, "replaceState");
    render(<SchoolSiftApp initialBootstrap={bootstrapConnected} />);
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Gmail account connected.",
    );
    expect(replaceSpy).toHaveBeenCalled();
    expect(window.location.search).not.toContain("connected=");
    replaceSpy.mockRestore();
  });

  it("disconnects an account via the exact endpoint", async () => {
    const { disconnectConnection, getBootstrap } = await api();
    vi.mocked(getBootstrap).mockResolvedValue(bootstrapReady);
    render(<SchoolSiftApp initialBootstrap={bootstrapConnected} />);
    await userEvent.click(
      screen.getByRole("button", { name: /^Accounts/ }),
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Disconnect account" }),
    );
    await waitFor(() =>
      expect(disconnectConnection).toHaveBeenCalledWith("conn-1"),
    );
  });

  it("shows the honest waiting state when connected with no packets", () => {
    render(<SchoolSiftApp initialBootstrap={bootstrapConnected} />);
    expect(
      screen.getByText(/press Sync inbox to look for school mail/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/No Action Packets yet/i)).toBeInTheDocument();
  });

  it("syncs the inbox via the exact endpoint", async () => {
    const { syncConnection, getBootstrap } = await api();
    vi.mocked(syncConnection).mockResolvedValue({
      discovered: 2,
      imported: 0,
      awaiting_source_confirmation: 2,
      failed: 0,
      next_cursor: null,
    });
    vi.mocked(getBootstrap).mockResolvedValue(bootstrapWithSource);
    render(<SchoolSiftApp initialBootstrap={bootstrapConnected} />);
    await userEvent.click(
      screen.getByRole("button", { name: /^Accounts/ }),
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Sync inbox" }),
    );
    await waitFor(() =>
      expect(syncConnection).toHaveBeenCalledWith("conn-1"),
    );
  });

  it("lists suggested senders and confirms one", async () => {
    const { confirmSource, getBootstrap } = await api();
    vi.mocked(confirmSource).mockResolvedValue({
      ...suggestedSource,
      status: "confirmed",
    });
    vi.mocked(getBootstrap).mockResolvedValue({
      ...bootstrapWithSource,
      sources: [{ ...suggestedSource, status: "confirmed" }],
      messages: [
        { ...bootstrapWithSource.messages[0], status: "awaiting_agent" },
      ],
    });
    render(<SchoolSiftApp initialBootstrap={bootstrapWithSource} />);
    await userEvent.click(
      screen.getByRole("button", { name: /^Senders/ }),
    );
    expect(screen.getByText("office@maplegrove.example")).toBeInTheDocument();
    expect(
      screen.getByText(/reads full message content only for senders you trust/i),
    ).toBeInTheDocument();
    await userEvent.click(
      screen.getByRole("button", { name: "Trust sender" }),
    );
    await waitFor(() =>
      expect(confirmSource).toHaveBeenCalledWith("src-1"),
    );
    await userEvent.click(
      screen.getByRole("button", { name: /^Accounts/ }),
    );
    expect(await screen.findByText(/Waiting for agent$/)).toBeInTheDocument();
  });

  it("ignores a suggested sender", async () => {
    const { rejectSource, getBootstrap } = await api();
    vi.mocked(rejectSource).mockResolvedValue({
      ...suggestedSource,
      status: "rejected",
    });
    vi.mocked(getBootstrap).mockResolvedValue(bootstrapConnected);
    render(<SchoolSiftApp initialBootstrap={bootstrapWithSource} />);
    await userEvent.click(
      screen.getByRole("button", { name: /^Senders/ }),
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Ignore sender" }),
    );
    await waitFor(() =>
      expect(rejectSource).toHaveBeenCalledWith("src-1"),
    );
  });

  it("shows a reconnect control when reauthorization is required", async () => {
    render(
      <SchoolSiftApp
        initialBootstrap={{
          ...bootstrapConnected,
          connections: [
            { ...bootstrapConnected.connections[0], status: "reauthorization_required" },
          ],
        }}
      />,
    );
    await userEvent.click(
      screen.getByRole("button", { name: /^Accounts/ }),
    );
    expect(
      screen.getByRole("button", { name: "Reconnect Gmail" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Sync inbox" }),
    ).not.toBeInTheDocument();
  });
});

describe("SchoolSiftApp packets", () => {
  it("lists a packet and shows the approve control after selection", async () => {
    render(<SchoolSiftApp initialBootstrap={bootstrapWithPacket} />);
    await userEvent.click(
      screen.getByRole("button", { name: /field trip/i }),
    );
    expect(
      screen.getByRole("button", { name: "Approve reply" }),
    ).toBeInTheDocument();
    expect(screen.getByText("For Maya")).toBeInTheDocument();
  });

  it("shows the approved state after approval", async () => {
    const { approveProposal, getBootstrap } = await api();
    const approved = {
      ...bootstrapWithPacket,
      packets: [
        {
          ...bootstrapWithPacket.packets[0],
          proposals: [
            { ...bootstrapWithPacket.packets[0].proposals[0], status: "approved" as const },
          ],
        },
      ],
    };
    vi.mocked(approveProposal).mockResolvedValue({
      proposal: approved.packets[0].proposals[0],
      execution: null,
    });
    vi.mocked(getBootstrap).mockResolvedValue(approved);
    render(<SchoolSiftApp initialBootstrap={bootstrapWithPacket} />);
    await userEvent.click(screen.getByRole("button", { name: /field trip/i }));
    await userEvent.click(screen.getByRole("button", { name: "Approve reply" }));
    expect(
      await screen.findByText(/queued for delivery/i),
    ).toBeInTheDocument();
  });

  it("shows which attachments were read, marking cited ones", async () => {
    const { container } = render(
      <SchoolSiftApp initialBootstrap={bootstrapWithPacket} />,
    );
    await userEvent.click(
      screen.getByRole("button", { name: /field trip/i }),
    );
    expect(screen.getByText("Read:")).toBeInTheDocument();
    const chips = Array.from(
      container.querySelectorAll(".attachment-chip"),
    ) as HTMLElement[];
    expect(chips.map((c) => c.textContent)).toEqual([
      "trip-letter.pdf",
      "lunch-menu.docx",
    ]);
    expect(chips[0]).toHaveClass("is-cited");
    expect(chips[1]).not.toHaveClass("is-cited");
  });

  it("labels evidence sources as Email for the body or the filename", async () => {
    const { container } = render(
      <SchoolSiftApp initialBootstrap={bootstrapWithPacket} />,
    );
    await userEvent.click(
      screen.getByRole("button", { name: /field trip/i }),
    );
    const sources = Array.from(
      container.querySelectorAll(".evidence-source"),
    ).map((el) => el.textContent);
    expect(sources).toEqual(["Email", "trip-letter.pdf"]);
    expect(screen.queryByText("body")).not.toBeInTheDocument();
  });

  it("re-runs the analysis through the API and warns proposals are discarded", async () => {
    const { reanalyzeMessage, getBootstrap } = await api();
    vi.mocked(reanalyzeMessage).mockResolvedValue({
      ...inboxMessage,
      id: "msg-1",
      status: "awaiting_agent" as const,
    });
    vi.mocked(getBootstrap).mockResolvedValue(bootstrapAwaitingAgent);
    render(<SchoolSiftApp initialBootstrap={bootstrapWithPacket} />);
    await userEvent.click(
      screen.getByRole("button", { name: /field trip/i }),
    );
    expect(
      screen.getByText(/current proposals are discarded/i),
    ).toBeInTheDocument();
    await userEvent.click(
      screen.getByRole("button", { name: "Re-analyze" }),
    );
    await waitFor(() =>
      expect(reanalyzeMessage).toHaveBeenCalledWith("msg-1"),
    );
  });

  it("groups the rail into decisions and information with status pills", () => {
    render(
      <SchoolSiftApp
        initialBootstrap={{
          ...bootstrapWithPacket,
          packets: [
            ...bootstrapWithPacket.packets,
            {
              ...bootstrapWithPacket.packets[0],
              id: "packet-info",
              subject: "Newsletter",
              information_only: true,
              urgency: "none" as const,
              proposals: [],
            },
          ],
        }}
      />,
    );
    expect(screen.getByText("Needs a decision")).toBeInTheDocument();
    expect(screen.getByText("For your information")).toBeInTheDocument();
    expect(screen.getByText("Soon")).toBeInTheDocument();
    expect(screen.getByText("Info")).toBeInTheDocument();
    expect(screen.getByText("1 to decide")).toBeInTheDocument();
  });

  it("shows only the sender display name on rail rows", () => {
    render(<SchoolSiftApp initialBootstrap={bootstrapWithPacket} />);
    const sender = screen.getByText("Maple Grove Office");
    expect(sender).toHaveAttribute("title", "office@maplegrove.example");
    expect(
      screen.queryByText(/Maple Grove Office </),
    ).not.toBeInTheDocument();
  });
});

describe("SchoolSiftApp resilience", () => {
  it("rejects malformed bootstrap data instead of casting", async () => {
    const { getBootstrap } = await api();
    const { bootstrapResponseSchema } = await import("../lib/contracts");
    vi.mocked(getBootstrap).mockImplementation(() =>
      Promise.resolve().then(() =>
        bootstrapResponseSchema.parse({ mode: "local", capabilities: null }),
      ),
    );
    render(<SchoolSiftApp />);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /Could not reach|went wrong/i,
    );
  });
});
