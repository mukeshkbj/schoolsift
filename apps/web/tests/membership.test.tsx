import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import SchoolSiftApp from "../components/SchoolSiftApp";
import type { BootstrapResponse } from "../lib/contracts";
import {
  bootstrapReady,
  ownerMembership,
  viewerMembership,
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

const secondMembership = {
  ...ownerMembership,
  household_id: "hh-2",
  role: "editor" as const,
};

const chooserBoot: BootstrapResponse = {
  ...bootstrapReady,
  household: null,
  active_household_id: null,
  memberships: [ownerMembership, secondMembership],
  members: [],
};

const viewerBoot: BootstrapResponse = {
  ...bootstrapReady,
  memberships: [viewerMembership],
  members: [],
};

describe("household chooser", () => {
  beforeEach(() => {
    sessionStorage.clear();
  });

  it("shows the chooser when multiple memberships and none active", () => {
    render(<SchoolSiftApp initialBootstrap={chooserBoot} />);
    expect(
      screen.getByRole("heading", { name: /Choose a household/i }),
    ).toBeInTheDocument();
    expect(screen.getByText("Household hh-1")).toBeInTheDocument();
    expect(screen.getByText("Household hh-2")).toBeInTheDocument();
  });

  it("selecting a household stores it and refreshes", async () => {
    const { getBootstrap } = await api();
    vi.mocked(getBootstrap).mockResolvedValue(bootstrapReady);
    render(<SchoolSiftApp initialBootstrap={chooserBoot} />);
    await userEvent.click(screen.getByText("Household hh-2"));
    await waitFor(() =>
      expect(sessionStorage.getItem("schoolsift.active-household")).toBe(
        "hh-2",
      ),
    );
  });

  it("accepting an invite sets the returned household active", async () => {
    const { acceptInvitation, getBootstrap } = await api();
    vi.mocked(acceptInvitation).mockResolvedValue({
      membership: secondMembership,
    });
    vi.mocked(getBootstrap).mockResolvedValue(bootstrapReady);
    render(<SchoolSiftApp initialBootstrap={chooserBoot} />);
    await userEvent.type(
      screen.getByLabelText(/Paste the token/i),
      "invite-token",
    );
    await userEvent.click(
      screen.getByRole("button", { name: /Accept invitation/i }),
    );
    await waitFor(() =>
      expect(acceptInvitation).toHaveBeenCalledWith("invite-token"),
    );
    await waitFor(() =>
      expect(sessionStorage.getItem("schoolsift.active-household")).toBe(
        "hh-2",
      ),
    );
  });
});

describe("caregivers section", () => {
  it("owner sees members and invite controls", () => {
    render(<SchoolSiftApp initialBootstrap={bootstrapReady} />);
    expect(
      screen.getByRole("heading", { name: "Caregivers" }),
    ).toBeInTheDocument();
    expect(screen.getByText("local@schoolsift.invalid")).toBeInTheDocument();
    expect(
      screen.getByLabelText("Invite caregiver email"),
    ).toBeInTheDocument();
  });

  it("creating an invite shows the one-time token", async () => {
    const { createInvitation } = await api();
    vi.mocked(createInvitation).mockResolvedValue({
      invitation: {
        id: "inv-1",
        household_id: "hh-1",
        email: "g@example.com",
        role: "editor",
        status: "pending",
        expires_at: "2026-09-20T00:00:00Z",
        created_at: "2026-09-13T00:00:00Z",
      },
      token: "one-time-token",
    });
    render(<SchoolSiftApp initialBootstrap={bootstrapReady} />);
    await userEvent.type(
      screen.getByLabelText("Invite caregiver email"),
      "g@example.com",
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Create invite" }),
    );
    await waitFor(() =>
      expect(
        screen.getByLabelText("Invitation token"),
      ).toBeInTheDocument(),
    );
    expect(screen.getByLabelText("Invitation token")).toHaveValue(
      "one-time-token",
    );
    expect(screen.getByText(/No email is sent yet/)).toBeInTheDocument();
  });

  it("non-owners do not see caregivers controls", () => {
    render(<SchoolSiftApp initialBootstrap={viewerBoot} />);
    expect(
      screen.queryByRole("heading", { name: "Caregivers" }),
    ).not.toBeInTheDocument();
  });
});

describe("viewer role", () => {
  it("hides mutation controls", () => {
    render(<SchoolSiftApp initialBootstrap={viewerBoot} />);
    expect(
      screen.queryByRole("button", { name: /Connect Gmail/i }),
    ).not.toBeInTheDocument();
  });
});

describe("api client", () => {
  it("sends bearer token and active household headers", async () => {
    sessionStorage.setItem("schoolsift.id-token", "tok-1");
    sessionStorage.setItem("schoolsift.active-household", "hh-1");
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => bootstrapReady,
    });
    vi.stubGlobal("fetch", fetchMock);
    const { getBootstrap } = await vi.importActual<
      typeof import("../lib/api")
    >("../lib/api");
    await getBootstrap();
    const headers = fetchMock.mock.calls[0][1].headers;
    expect(headers.authorization).toBe("Bearer tok-1");
    expect(headers["x-schoolsift-household"]).toBe("hh-1");
    sessionStorage.clear();
    vi.unstubAllGlobals();
  });
});
