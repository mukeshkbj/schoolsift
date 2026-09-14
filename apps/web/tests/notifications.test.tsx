import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import SchoolSiftApp from "../components/SchoolSiftApp";
import type { BootstrapResponse } from "../lib/contracts";
import { ApiError } from "../lib/errors";
import {
  bootstrapConnected,
  gmailConnection,
  viewerMembership,
} from "./helpers";

vi.mock("../lib/api", async () => {
  const { bootstrapConnected, child, household } = await import("./helpers");
  return {
    getBootstrap: vi.fn().mockResolvedValue(bootstrapConnected),
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

const boot = (over: Partial<BootstrapResponse> = {}): BootstrapResponse => ({
  ...bootstrapConnected,
  ...over,
});

describe("notification controls", () => {
  it("offers Enable notifications when no subscription exists", async () => {
    render(<SchoolSiftApp initialBootstrap={boot()} />);
    const enable = await screen.findByRole("button", {
      name: "Enable notifications",
    });
    const mod = await api();
    vi.mocked(mod.enableNotifications).mockResolvedValue({
      provider: "gmail",
      status: "active",
      expires_at: "2030-01-01T00:00:00Z",
    });
    await userEvent.click(enable);
    await waitFor(() =>
      expect(mod.enableNotifications).toHaveBeenCalledWith("conn-1"),
    );
  });

  it("offers Renew notifications when subscription is due", async () => {
    render(
      <SchoolSiftApp
        initialBootstrap={boot({
          connections: [
            {
              ...gmailConnection,
              subscription: {
                provider: "gmail",
                status: "renewal_due",
                expires_at: "2026-09-13T00:00:00Z",
              },
            },
          ],
        })}
      />,
    );
    expect(
      await screen.findByRole("button", { name: "Renew notifications" }),
    ).toBeInTheDocument();
  });

  it("shows active notification status without a button", async () => {
    render(
      <SchoolSiftApp
        initialBootstrap={boot({
          connections: [
            {
              ...gmailConnection,
              subscription: {
                provider: "gmail",
                status: "active",
                expires_at: "2030-01-01T00:00:00Z",
              },
            },
          ],
        })}
      />,
    );
    await screen.findByText("caregiver@example.com");
    expect(screen.getByText(/notifications on/)).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /notifications/i }),
    ).not.toBeInTheDocument();
  });

  it("hides notification controls from viewers", async () => {
    render(
      <SchoolSiftApp
        initialBootstrap={boot({ memberships: [viewerMembership] })}
      />,
    );
    await screen.findByText("caregiver@example.com");
    expect(
      screen.queryByRole("button", { name: /notifications/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Sync inbox" }),
    ).not.toBeInTheDocument();
  });

  it("surfaces the provider setup explanation when unavailable", async () => {
    const mod = await api();
    vi.mocked(mod.enableNotifications).mockRejectedValue(
      new ApiError(
        503,
        "WEBHOOK_NOT_CONFIGURED",
        "Webhook notifications require an HTTPS public API URL.",
      ),
    );
    render(<SchoolSiftApp initialBootstrap={boot()} />);
    await userEvent.click(
      await screen.findByRole("button", { name: "Enable notifications" }),
    );
    await waitFor(() =>
      expect(
        screen.getByText(/HTTPS public API URL/),
      ).toBeInTheDocument(),
    );
  });
});
