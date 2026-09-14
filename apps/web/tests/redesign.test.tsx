import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import SchoolSiftApp from "../components/SchoolSiftApp";
import {
  bootstrapConnected,
  bootstrapReady,
  household,
} from "./helpers";
import type { SchoolSource } from "../lib/contracts";

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
    listMembers: vi.fn(),
    listInvitations: vi.fn(),
    createInvitation: vi.fn(),
    revokeInvitation: vi.fn(),
    acceptInvitation: vi.fn(),
    changeMemberRole: vi.fn(),
    removeMember: vi.fn(),
    listExecutions: vi.fn(),
    dispatchExecutions: vi.fn(),
    runExecution: vi.fn(),
  };
});

const api = () => import("../lib/api");

const source = (
  id: string,
  email: string,
  status: SchoolSource["status"] = "suggested",
): SchoolSource => ({
  id,
  household_id: "hh-1",
  connection_id: "conn-1",
  sender_email: email,
  sender_domain: email.split("@")[1],
  status,
  first_seen_at: "2026-09-13T00:00:00Z",
  last_seen_at: "2026-09-13T00:00:00Z",
});

const manySources = (n: number): SchoolSource[] =>
  Array.from({ length: n }, (_, i) =>
    source(`src-${i}`, `person${i}@school${Math.floor(i / 5)}.example`),
  );

const openSettingsItem = async (name: string) => {
  await userEvent.click(
    screen.getByRole("button", { name: /Settings/ }),
  );
  await userEvent.click(screen.getByRole("menuitem", { name }));
};

const drawer = () => screen.getByRole("dialog", { name: "Settings" });

describe("view navigation", () => {
  it("switches between To review, Senders, and Accounts", async () => {
    render(
      <SchoolSiftApp
        initialBootstrap={{
          ...bootstrapConnected,
          sources: [source("s1", "office@maplegrove.example")],
        }}
      />,
    );
    expect(
      screen.getByRole("heading", { name: "What needs you" }),
    ).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /^Senders/ }));
    expect(
      screen.getByRole("heading", { name: "Senders" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("office@maplegrove.example"),
    ).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /^Accounts/ }));
    expect(
      screen.getByText("caregiver@example.com"),
    ).toBeInTheDocument();

    await userEvent.click(
      screen.getByRole("button", { name: /^To review/ }),
    );
    expect(
      screen.getByRole("heading", { name: "What needs you" }),
    ).toBeInTheDocument();
  });
});

describe("senders view", () => {
  const bootWithSources = (sources: SchoolSource[]) => ({
    ...bootstrapConnected,
    sources,
  });

  it("filters by address and domain via search", async () => {
    render(
      <SchoolSiftApp
        initialBootstrap={bootWithSources([
          source("s1", "office@maplegrove.example"),
          source("s2", "pta@maplegrove.example"),
          source("s3", "news@otherdistrict.example"),
        ])}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: /^Senders/ }));
    await userEvent.type(
      screen.getByLabelText("Search senders"),
      "otherdistrict",
    );
    expect(
      screen.getByText("news@otherdistrict.example"),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("office@maplegrove.example"),
    ).not.toBeInTheDocument();
  });

  it("groups senders by domain with counts", async () => {
    render(
      <SchoolSiftApp
        initialBootstrap={bootWithSources([
          source("s1", "office@maplegrove.example"),
          source("s2", "pta@maplegrove.example"),
          source("s3", "news@otherdistrict.example"),
        ])}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: /^Senders/ }));
    const group = screen.getByRole("region", {
      name: "maplegrove.example",
    });
    expect(within(group).getByText("2 senders")).toBeInTheDocument();
    expect(
      within(group).getByRole("button", { name: "Trust all in domain" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("region", { name: "otherdistrict.example" }),
    ).toBeInTheDocument();
  });

  it("paginates 120 sources with Show 50 more", async () => {
    render(
      <SchoolSiftApp
        initialBootstrap={bootWithSources(manySources(120))}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: /^Senders/ }));
    expect(
      document.querySelectorAll(".senders-pane .sender-row"),
    ).toHaveLength(50);
    await userEvent.click(
      screen.getByRole("button", { name: "Show 50 more" }),
    );
    expect(
      document.querySelectorAll(".senders-pane .sender-row"),
    ).toHaveLength(100);
    await userEvent.click(
      screen.getByRole("button", { name: "Show 50 more" }),
    );
    expect(
      document.querySelectorAll(".senders-pane .sender-row"),
    ).toHaveLength(120);
    expect(
      screen.queryByRole("button", { name: "Show 50 more" }),
    ).not.toBeInTheDocument();
  });

  it("collapses domains with more than 5 senders behind a toggle", async () => {
    render(
      <SchoolSiftApp
        initialBootstrap={bootWithSources(
          Array.from({ length: 8 }, (_, i) =>
            source(`big-${i}`, `staff${i}@bigschool.example`),
          ).concat([source("solo", "office@tiny.example")]),
        )}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: /^Senders/ }));
    const big = screen.getByRole("region", { name: "bigschool.example" });
    expect(within(big).getByText("8 senders")).toBeInTheDocument();
    expect(big.querySelectorAll(".sender-row")).toHaveLength(0);
    await userEvent.click(
      within(big).getByRole("button", { name: "Show 8 senders" }),
    );
    expect(big.querySelectorAll(".sender-row")).toHaveLength(8);
    await userEvent.click(
      within(big).getByRole("button", { name: "Show fewer" }),
    );
    expect(big.querySelectorAll(".sender-row")).toHaveLength(0);
    const tiny = screen.getByRole("region", { name: "tiny.example" });
    expect(tiny.querySelectorAll(".sender-row")).toHaveLength(1);
    expect(
      within(tiny).queryByRole("button", { name: /Show .* senders/ }),
    ).not.toBeInTheDocument();
  });
});

describe("settings drawer", () => {
  it("opens via the Settings menu at the Children section", async () => {
    render(<SchoolSiftApp initialBootstrap={bootstrapReady} />);
    await openSettingsItem("Add child");
    const d = drawer();
    expect(d).toHaveAttribute("aria-modal", "true");
    expect(
      within(d).getByRole("heading", { name: "Children" }),
    ).toBeInTheDocument();
    expect(within(d).getByText("Maya")).toBeInTheDocument();
    expect(within(d).getByLabelText("Child name")).toBeInTheDocument();
  });

  it("adds a second child from the drawer", async () => {
    const { addChild, getBootstrap } = await api();
    vi.mocked(getBootstrap).mockResolvedValue(bootstrapReady);
    render(<SchoolSiftApp initialBootstrap={bootstrapReady} />);
    await openSettingsItem("Add child");
    const d = drawer();
    await userEvent.type(within(d).getByLabelText("Child name"), "Theo");
    await userEvent.type(within(d).getByLabelText("School"), "River Elementary");
    await userEvent.type(within(d).getByLabelText("Grade"), "1");
    await userEvent.click(
      within(d).getByRole("button", { name: "Add child" }),
    );
    await waitFor(() =>
      expect(addChild).toHaveBeenCalledWith("Theo", "River Elementary", "1"),
    );
  });

  it("invites a caregiver from the drawer", async () => {
    const { createInvitation, getBootstrap } = await api();
    vi.mocked(createInvitation).mockResolvedValue({
      invitation: {
        id: "inv-1",
        household_id: "hh-1",
        email: "grandma@example.org",
        role: "editor",
        status: "pending",
        created_at: "2026-09-13T00:00:00Z",
        expires_at: "2026-09-20T00:00:00Z",
      },
      token: "one-time-token",
    });
    vi.mocked(getBootstrap).mockResolvedValue(bootstrapReady);
    render(<SchoolSiftApp initialBootstrap={bootstrapReady} />);
    await openSettingsItem("Invite caregiver");
    const d = drawer();
    await userEvent.type(
      within(d).getByLabelText("Invite caregiver email"),
      "grandma@example.org",
    );
    await userEvent.click(
      within(d).getByRole("button", { name: "Create invite" }),
    );
    await waitFor(() =>
      expect(createInvitation).toHaveBeenCalledWith(
        "grandma@example.org",
        "editor",
      ),
    );
    expect(await within(d).findByLabelText("Invitation token")).toHaveValue(
      "one-time-token",
    );
  });

  it("closes on Escape and restores focus to the Settings button", async () => {
    render(<SchoolSiftApp initialBootstrap={bootstrapReady} />);
    await openSettingsItem("Household");
    expect(drawer()).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Settings/ }),
    ).toHaveFocus();
  });

  it("shows household name and time zone read-only", async () => {
    render(<SchoolSiftApp initialBootstrap={bootstrapReady} />);
    await openSettingsItem("Household");
    const d = drawer();
    expect(within(d).getByText(household.name)).toBeInTheDocument();
    expect(within(d).getByText(household.timezone)).toBeInTheDocument();
    expect(
      within(d).queryByLabelText("Household name"),
    ).not.toBeInTheDocument();
  });
});
