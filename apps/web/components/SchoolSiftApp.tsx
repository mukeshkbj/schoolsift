"use client";

import { useCallback, useEffect, useState } from "react";
import {
  clearSession,
  cognitoConfigured,
  setActiveHousehold,
  signOutUrl,
} from "../lib/auth";
import { ApiError } from "../lib/errors";
import {
  acceptInvitation,
  addChild,
  approveProposal,
  dispatchExecutions,
  changeMemberRole,
  confirmSource,
  createHousehold,
  createInvitation,
  disconnectConnection,
  editProposal,
  enableNotifications,
  getBootstrap,
  processMessage,
  runExecution,
  rejectProposal,
  rejectSource,
  removeMember,
  revokeInvitation,
  startConnect,
  syncConnection,
} from "../lib/api";
import type {
  BootstrapResponse,
  ExecutionRecord,
  ProposalPayload,
  ProposalVersion,
} from "../lib/contracts";
import { headProposals } from "../lib/contracts";
import ActionPacketView from "./ActionPacket";

type Props = {
  initialBootstrap?: BootstrapResponse | null;
  onNavigate?: (url: string) => void;
};

const PROVIDERS = ["gmail", "outlook"] as const;
type Provider = (typeof PROVIDERS)[number];

const PROVIDER_LABEL: Record<Provider, string> = {
  gmail: "Gmail",
  outlook: "Outlook",
};

const MISSING_ENV: Record<Provider, string[]> = {
  gmail: ["GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET"],
  outlook: ["OUTLOOK_CLIENT_ID", "OUTLOOK_CLIENT_SECRET"],
};

export default function SchoolSiftApp({
  initialBootstrap = null,
  onNavigate = (url) => window.location.assign(url),
}: Props) {
  const [boot, setBoot] = useState<BootstrapResponse | null>(initialBootstrap);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [loading, setLoading] = useState(initialBootstrap === null);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [pageError, setPageError] = useState<string | null>(null);
  const [connectedNote, setConnectedNote] = useState<string | null>(null);
  const [conflicts, setConflicts] = useState<Record<string, string>>({});
  const [inviteToken, setInviteToken] = useState<string | null>(null);
  const [inviteCopied, setInviteCopied] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setBoot(await getBootstrap());
      setPageError(null);
    } catch (e) {
      setPageError(
        e instanceof ApiError
          ? e.message
          : "Could not reach the SchoolSift API on :8000.",
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (initialBootstrap === null) void refresh();
    const params = new URLSearchParams(window.location.search);
    const connected = params.get("connected");
    if (connected === "gmail" || connected === "outlook") {
      setConnectedNote(`${PROVIDER_LABEL[connected]} account connected.`);
      params.delete("connected");
      const next = params.size > 0 ? `?${params}` : window.location.pathname;
      window.history.replaceState(null, "", next);
    }
  }, [initialBootstrap, refresh]);

  async function run(mutate: () => Promise<unknown>, key: string) {
    setBusyKey(key);
    try {
      await mutate();
      setConflicts((c) => {
        const next = { ...c };
        delete next[key];
        return next;
      });
      await refresh();
    } catch (e) {
      if (e instanceof ApiError && e.stale) {
        setConflicts((c) => ({ ...c, [key]: e.message }));
      } else {
        setPageError(e instanceof ApiError ? e.message : "Something went wrong.");
      }
    } finally {
      setBusyKey(null);
    }
  }

  async function onConnect(provider: Provider) {
    const key = `connect:${provider}`;
    setBusyKey(key);
    try {
      const { authorization_url } = await startConnect(provider);
      onNavigate(authorization_url);
    } catch (e) {
      setPageError(
        e instanceof ApiError ? e.message : "Connection attempt failed.",
      );
      setBusyKey(null);
    }
  }

  const onDisconnect = (connectionId: string) =>
    run(() => disconnectConnection(connectionId), `disconnect:${connectionId}`);

  const onSync = (connectionId: string) =>
    run(() => syncConnection(connectionId), `sync:${connectionId}`);

  const onNotifications = (connectionId: string) =>
    run(
      () => enableNotifications(connectionId),
      `notifications:${connectionId}`,
    );

  const onConfirmSource = (sourceId: string) =>
    run(() => confirmSource(sourceId), `source-confirm:${sourceId}`);

  const onRejectSource = (sourceId: string) =>
    run(() => rejectSource(sourceId), `source-reject:${sourceId}`);

  const onProcess = (messageId: string) =>
    run(async () => {
      const packet = await processMessage(messageId);
      setSelectedId(packet.id);
    }, `process:${messageId}`);

  const onSave = (v: ProposalVersion, payload: ProposalPayload) =>
    run(() => editProposal(v.id, v.version, payload), `${v.id}:edit`);

  const onApprove = (v: ProposalVersion) =>
    run(
      () => approveProposal(v.id, v.version, v.payload_hash),
      `${v.id}:approve`,
    );

  const onReject = (v: ProposalVersion) =>
    run(
      () => rejectProposal(v.id, v.version, v.payload_hash),
      `${v.id}:reject`,
    );

  const onDispatch = () => run(() => dispatchExecutions(), "exec:dispatch");

  const onRun = (execution: ExecutionRecord) =>
    run(() => runExecution(execution.id), `exec:run:${execution.id}`);

  const onSelectHousehold = (householdId: string) =>
    run(async () => {
      setActiveHousehold(householdId);
    }, "select-household");

  const onAcceptInvite = (token: string) =>
    run(async () => {
      const res = await acceptInvitation(token);
      setActiveHousehold(res.membership.household_id);
    }, "accept-invite");

  const onInvite = (email: string, role: "editor" | "viewer") =>
    run(async () => {
      const res = await createInvitation(email, role);
      setInviteToken(res.token);
      setInviteCopied(false);
    }, "invite");

  const onRevokeInvite = (invitationId: string) =>
    run(async () => {
      await revokeInvitation(invitationId);
      setInviteToken(null);
    }, `revoke:${invitationId}`);

  const onChangeRole = (userId: string, role: string) =>
    run(() => changeMemberRole(userId, role), `role:${userId}`);

  const onRemoveMember = (userId: string) =>
    run(() => removeMember(userId), `remove:${userId}`);

  const onSignOut = () => {
    clearSession();
    onNavigate(signOutUrl());
  };

  const packets = boot?.packets ?? [];
  const executions = boot?.executions ?? [];
  const connections = boot?.connections ?? [];
  const sources = boot?.sources ?? [];
  const messages = boot?.messages ?? [];
  const suggestedSources = sources.filter((s) => s.status === "suggested");
  const confirmedCount = sources.filter((s) => s.status === "confirmed").length;
  const selected = packets.find((p) => p.id === selectedId);
  const activeMembership =
    boot?.memberships.find(
      (m) => m.household_id === boot.active_household_id,
    ) ?? null;
  const role = activeMembership?.role ?? "owner";
  const canEdit = role !== "viewer";
  const isOwner = role === "owner";

  const MESSAGE_STATUS: Record<string, string> = {
    awaiting_source: "Waiting for sender review",
    awaiting_agent: "Waiting for agent",
    processed: "Processed",
    failed: "Needs manual review",
  };

  return (
    <div className="app-shell">
      <header className="app-topbar">
        <div className="app-brand">
          <span className="app-wordmark">SchoolSift</span>
          <span className="app-mode">{boot?.mode ?? "local"} mode</span>
        </div>
        {cognitoConfigured() && (
          <button
            type="button"
            className="btn btn-quiet"
            onClick={onSignOut}
          >
            Sign out
          </button>
        )}
      </header>

      {pageError !== null && (
        <p role="alert" className="app-error">
          {pageError}
        </p>
      )}

      {loading ? (
        <p className="app-status" role="status">
          Loading SchoolSift…
        </p>
      ) : boot === null ? null : boot.household === null &&
        boot.memberships.length > 0 ? (
        <section className="app-empty" aria-label="Choose a household">
          <h1 className="app-empty-title">Choose a household</h1>
          <p>Select which household you want to work in.</p>
          <ol className="connection-list household-list">
            {boot.memberships.map((m) => (
              <li key={m.household_id} className="connection-item">
                <button
                  type="button"
                  className="btn btn-secondary"
                  disabled={busyKey === "select-household"}
                  onClick={() => void onSelectHousehold(m.household_id)}
                >
                  Household {m.household_id}
                </button>
                <span className="connection-meta">{m.role}</span>
              </li>
            ))}
          </ol>
          <InviteAcceptForm busy={busyKey === "accept-invite"} onAccept={onAcceptInvite} />
        </section>
      ) : boot.household === null ? (
        <section className="app-empty" aria-label="Household setup">
          <h1 className="app-empty-title">Set up your household</h1>
          <p>
            SchoolSift keeps everything local until you connect a school inbox.
            Start by naming this household.
          </p>
          <form
            className="setup-form"
            onSubmit={(e) => {
              e.preventDefault();
              const fd = new FormData(e.currentTarget);
              void run(
                () =>
                  createHousehold(
                    String(fd.get("name") ?? ""),
                    String(fd.get("timezone") ?? ""),
                  ),
                "household",
              );
            }}
          >
            <label className="field">
              <span className="field-label">Household name</span>
              <input name="name" type="text" required autoComplete="off" />
            </label>
            <label className="field">
              <span className="field-label">Time zone (IANA name)</span>
              <input
                name="timezone"
                type="text"
                required
                defaultValue={Intl.DateTimeFormat().resolvedOptions().timeZone}
              />
            </label>
            <button
              type="submit"
              className="btn btn-primary"
              disabled={busyKey === "household"}
              aria-busy={busyKey === "household"}
            >
              Create household
              {busyKey === "household" && (
                <span className="btn-busy" aria-hidden="true" />
              )}
            </button>
          </form>
          <InviteAcceptForm busy={busyKey === "accept-invite"} onAccept={onAcceptInvite} />
        </section>
      ) : boot.children.length === 0 ? (
        <section className="app-empty" aria-label="Add a child">
          <h1 className="app-empty-title">Add your first child</h1>
          <p>
            SchoolSift uses names, schools, and grades to match incoming mail to
            the right kid.
          </p>
          <form
            className="setup-form"
            onSubmit={(e) => {
              e.preventDefault();
              const fd = new FormData(e.currentTarget);
              void run(
                () =>
                  addChild(
                    String(fd.get("name") ?? ""),
                    String(fd.get("school") ?? ""),
                    String(fd.get("grade") ?? ""),
                  ),
                "child",
              );
            }}
          >
            <label className="field">
              <span className="field-label">Child name</span>
              <input name="name" type="text" required autoComplete="off" />
            </label>
            <label className="field">
              <span className="field-label">School</span>
              <input name="school" type="text" required autoComplete="off" />
            </label>
            <label className="field">
              <span className="field-label">Grade</span>
              <input name="grade" type="text" required autoComplete="off" />
            </label>
            <button
              type="submit"
              className="btn btn-primary"
              disabled={busyKey === "child"}
              aria-busy={busyKey === "child"}
            >
              Add child
              {busyKey === "child" && (
                <span className="btn-busy" aria-hidden="true" />
              )}
            </button>
          </form>
        </section>
      ) : (
        <div className="workspace">
          <nav className="inbox-rail" aria-label="Action packets">
            <h2 className="rail-heading">Action Packets</h2>
            {packets.length === 0 ? (
              <p className="rail-empty">
                Nothing to review yet — packets appear after a connected inbox
                syncs school mail.
              </p>
            ) : (
              <ol className="rail-list">
                {packets.map((p) => {
                  const pending = headProposals(p).filter(
                    (v) => v.status === "proposed",
                  ).length;
                  return (
                    <li key={p.id} className="rail-item">
                      <span className="sift-mark" aria-hidden="true" />
                      <button
                        type="button"
                        className={`rail-message${p.id === selectedId ? " is-selected" : ""}`}
                        onClick={() => setSelectedId(p.id)}
                        aria-current={p.id === selectedId ? "true" : undefined}
                      >
                        <span className="rail-sender">{p.sender}</span>
                        <span className="rail-subject">{p.subject}</span>
                        <span className="rail-meta">
                          {pending > 0
                            ? `${pending} proposal${pending === 1 ? "" : "s"} to review`
                            : p.information_only
                              ? "Information only"
                              : "Decided"}
                        </span>
                      </button>
                    </li>
                  );
                })}
              </ol>
            )}
          </nav>

          <section className="packet-pane" aria-label="Action packet detail">
            {selected === undefined ? (
              <div className="packet-placeholder">
                <p>
                  {packets.length === 0
                    ? "No Action Packets yet."
                    : "Select a packet to review it."}
                </p>
              </div>
            ) : (
              <ActionPacketView
                packet={selected}
                executions={executions}
                localMode={boot.mode === "local"}
                busyKey={busyKey}
                conflicts={conflicts}
                readOnly={!canEdit}
                onSave={onSave}
                onApprove={onApprove}
                onReject={onReject}
                onReload={refresh}
                onDispatch={onDispatch}
                onRun={onRun}
              />
            )}
          </section>

          <aside className="accounts-pane" aria-label="School accounts">
            <h2 className="rail-heading">School accounts</h2>
            {PROVIDERS.some((p) => !boot.capabilities[p]) && (
              <div className="setup-card">
                <p>
                  Inbox connection isn&apos;t configured yet. Set these
                  environment variables on the API, then restart it:
                </p>
                <ul className="setup-env-list">
                  {PROVIDERS.filter((p) => !boot.capabilities[p]).map((p) => (
                    <li key={p}>
                      {MISSING_ENV[p].map((v) => (
                        <code key={v}>{v}</code>
                      ))}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            <div className="connect-actions">
              {canEdit &&
                PROVIDERS.map((p) => (
                <button
                  key={p}
                  type="button"
                  className="btn btn-secondary"
                  disabled={!boot.capabilities[p] || busyKey !== null}
                  aria-busy={busyKey === `connect:${p}`}
                  onClick={() => onConnect(p)}
                >
                  Connect {PROVIDER_LABEL[p]}
                  {busyKey === `connect:${p}` && (
                    <span className="btn-busy" aria-hidden="true" />
                  )}
                </button>
                ))}
            </div>
            {connectedNote !== null && (
              <p role="status" className="connect-note">
                {connectedNote}
              </p>
            )}
            {connections.length > 0 ? (
              <ol className="connection-list">
                {connections.map((c) => (
                  <li key={c.id} className="connection-item">
                    <span className="connection-email">{c.email}</span>
                    <span className="connection-meta">
                      {PROVIDER_LABEL[c.provider]} ·{" "}
                      {c.status.replaceAll("_", " ")}
                      {c.sync_status === "failed"
                        ? " · sync failed"
                        : c.sync_status === "ready"
                          ? ""
                          : c.sync_status !== "idle"
                            ? ` · ${c.sync_status.replaceAll("_", " ")}`
                            : ""}
                      {c.last_sync_at
                        ? ` · synced ${new Date(c.last_sync_at).toLocaleString()}`
                        : " · not synced yet"}
                      {c.subscription?.status === "active"
                        ? ` · notifications on${
                            c.subscription.expires_at
                              ? ` until ${new Date(
                                  c.subscription.expires_at,
                                ).toLocaleString()}`
                              : ""
                          }`
                        : ""}
                    </span>
                    {c.status === "connected" &&
                      canEdit &&
                      c.subscription === null && (
                        <button
                          type="button"
                          className="btn btn-secondary"
                          disabled={busyKey === `notifications:${c.id}`}
                          aria-busy={busyKey === `notifications:${c.id}`}
                          onClick={() => void onNotifications(c.id)}
                        >
                          Enable notifications
                          {busyKey === `notifications:${c.id}` && (
                            <span className="btn-busy" aria-hidden="true" />
                          )}
                        </button>
                      )}
                    {c.status === "connected" &&
                      canEdit &&
                      c.subscription !== null &&
                      c.subscription.status !== "active" && (
                        <button
                          type="button"
                          className="btn btn-secondary"
                          disabled={busyKey === `notifications:${c.id}`}
                          aria-busy={busyKey === `notifications:${c.id}`}
                          onClick={() => void onNotifications(c.id)}
                        >
                          Renew notifications
                          {busyKey === `notifications:${c.id}` && (
                            <span className="btn-busy" aria-hidden="true" />
                          )}
                        </button>
                      )}
                    {c.status === "reauthorization_required" && canEdit && (
                      <button
                        type="button"
                        className="btn btn-secondary"
                        disabled={
                          !boot.capabilities[c.provider] || busyKey !== null
                        }
                        onClick={() => onConnect(c.provider)}
                      >
                        Reconnect {PROVIDER_LABEL[c.provider]}
                      </button>
                    )}
                    {c.status === "connected" && canEdit && (
                      <button
                        type="button"
                        className="btn btn-secondary"
                        disabled={busyKey === `sync:${c.id}`}
                        aria-busy={busyKey === `sync:${c.id}`}
                        onClick={() => void onSync(c.id)}
                      >
                        Sync inbox
                        {busyKey === `sync:${c.id}` && (
                          <span className="btn-busy" aria-hidden="true" />
                        )}
                      </button>
                    )}
                    {canEdit && (
                    <button
                      type="button"
                      className="btn btn-quiet"
                      disabled={busyKey === `disconnect:${c.id}`}
                      aria-busy={busyKey === `disconnect:${c.id}`}
                      onClick={() => void onDisconnect(c.id)}
                    >
                      Disconnect account
                      {busyKey === `disconnect:${c.id}` && (
                        <span className="btn-busy" aria-hidden="true" />
                      )}
                    </button>
                    )}
                  </li>
                ))}
              </ol>
            ) : (
              <p className="connection-empty">
                No accounts connected. School mail stays untouched until you
                connect one.
              </p>
            )}
            {connections.length > 0 && packets.length === 0 && (
              <p className="connection-empty">
                Connected — press Sync inbox to look for school mail.
              </p>
            )}
            {suggestedSources.length > 0 && (
              <section
                className="source-review"
                aria-label="Review school senders"
              >
                <h3 className="source-heading">Review school senders</h3>
                <p className="connection-empty">
                  SchoolSift reads full message content only for senders you
                  trust. Everything else stays headers-only.
                </p>
                <ol className="connection-list">
                  {suggestedSources.map((s) => (
                    <li key={s.id} className="connection-item">
                      <span className="connection-email">{s.sender_email}</span>
                      <span className="connection-meta">{s.sender_domain}</span>
                      {canEdit && (
                      <div className="source-actions">
                        <button
                          type="button"
                          className="btn btn-primary"
                          disabled={busyKey === `source-confirm:${s.id}`}
                          aria-busy={busyKey === `source-confirm:${s.id}`}
                          onClick={() => void onConfirmSource(s.id)}
                        >
                          Trust sender
                          {busyKey === `source-confirm:${s.id}` && (
                            <span className="btn-busy" aria-hidden="true" />
                          )}
                        </button>
                        <button
                          type="button"
                          className="btn btn-quiet"
                          disabled={busyKey === `source-reject:${s.id}`}
                          aria-busy={busyKey === `source-reject:${s.id}`}
                          onClick={() => void onRejectSource(s.id)}
                        >
                          Ignore sender
                          {busyKey === `source-reject:${s.id}` && (
                            <span className="btn-busy" aria-hidden="true" />
                          )}
                        </button>
                      </div>
                      )}
                    </li>
                  ))}
                </ol>
              </section>
            )}
            {suggestedSources.length === 0 && confirmedCount > 0 && (
              <p className="connection-empty">
                {confirmedCount} trusted sender
                {confirmedCount === 1 ? "" : "s"}.
              </p>
            )}
            {messages.length > 0 && (
              <section className="source-review" aria-label="Inbox status">
                <h3 className="source-heading">Inbox</h3>
                <ol className="connection-list">
                  {messages.map((m) => (
                    <li key={m.id} className="connection-item">
                      <span className="connection-email">{m.subject}</span>
                      <span className="connection-meta">
                        {m.sender_email} ·{" "}
                        {MESSAGE_STATUS[m.status] ?? m.status}
                      </span>
                      {m.status === "failed" &&
                        m.manual_review_reason !== null && (
                          <span className="connection-meta">
                            {m.manual_review_reason}
                          </span>
                        )}
                      {m.status === "awaiting_agent" &&
                        (boot.capabilities.agent && canEdit ? (
                          <button
                            type="button"
                            className="btn btn-secondary"
                            disabled={busyKey === `process:${m.id}`}
                            aria-busy={busyKey === `process:${m.id}`}
                            onClick={() => void onProcess(m.id)}
                          >
                            Process message
                            {busyKey === `process:${m.id}` && (
                              <span className="btn-busy" aria-hidden="true" />
                            )}
                          </button>
                        ) : (
                          <span className="connection-meta">
                            Waiting for agent configuration — set{" "}
                            <code>SCHOOLSIFT_AWS_REGION</code> and{" "}
                            <code>SCHOOLSIFT_BEDROCK_MODEL_ID</code> on the API.
                          </span>
                        ))}
                    </li>
                  ))}
                </ol>
              </section>
            )}
            {isOwner && (
              <section className="source-review" aria-label="Caregivers">
                <h3 className="source-heading">Caregivers</h3>
                <ol className="connection-list">
                  {boot.members.map((m) => (
                    <li key={m.user_id} className="connection-item">
                      <span className="connection-email">{m.email}</span>
                      <span className="connection-meta">{m.role}</span>
                      <div className="source-actions">
                        <label className="field member-role">
                          <span className="field-label">Role</span>
                          <select
                            value={m.role}
                            disabled={busyKey === `role:${m.user_id}`}
                            onChange={(e) =>
                              void onChangeRole(m.user_id, e.target.value)
                            }
                          >
                            <option value="owner">owner</option>
                            <option value="editor">editor</option>
                            <option value="viewer">viewer</option>
                          </select>
                        </label>
                        <button
                          type="button"
                          className="btn btn-quiet"
                          disabled={busyKey === `remove:${m.user_id}`}
                          onClick={() => void onRemoveMember(m.user_id)}
                        >
                          Remove
                        </button>
                      </div>
                    </li>
                  ))}
                </ol>
                <form
                  className="setup-form"
                  onSubmit={(e) => {
                    e.preventDefault();
                    const fd = new FormData(e.currentTarget);
                    void onInvite(
                      String(fd.get("email") ?? ""),
                      fd.get("role") === "editor" ? "editor" : "viewer",
                    );
                    e.currentTarget.reset();
                  }}
                >
                  <label className="field">
                    <span className="field-label">Invite caregiver email</span>
                    <input name="email" type="email" required />
                  </label>
                  <label className="field">
                    <span className="field-label">Role</span>
                    <select name="role" defaultValue="editor">
                      <option value="editor">editor</option>
                      <option value="viewer">viewer</option>
                    </select>
                  </label>
                  <button
                    type="submit"
                    className="btn btn-secondary"
                    disabled={busyKey === "invite"}
                    aria-busy={busyKey === "invite"}
                  >
                    Create invite
                  </button>
                </form>
                {inviteToken !== null && (
                  <div className="invite-token-box">
                    <p className="connection-meta">
                      No email is sent yet — copy this invite link text and
                      share it with the caregiver. It is shown only once.
                    </p>
                    <input
                      readOnly
                      className="invite-token"
                      value={inviteToken}
                      aria-label="Invitation token"
                      onFocus={(e) => e.currentTarget.select()}
                    />
                    <button
                      type="button"
                      className="btn btn-secondary"
                      onClick={() => {
                        void navigator.clipboard
                          ?.writeText(inviteToken)
                          .then(() => setInviteCopied(true));
                      }}
                    >
                      {inviteCopied ? "Copied" : "Copy invite"}
                    </button>
                  </div>
                )}
                {boot.invitations.length > 0 && (
                  <ol className="connection-list">
                    {boot.invitations.map((i) => (
                      <li key={i.id} className="connection-item">
                        <span className="connection-email">{i.email}</span>
                        <span className="connection-meta">
                          {i.role} · {i.status}
                        </span>
                        {i.status === "pending" && (
                          <button
                            type="button"
                            className="btn btn-quiet"
                            disabled={busyKey === `revoke:${i.id}`}
                            onClick={() => void onRevokeInvite(i.id)}
                          >
                            Revoke invite
                          </button>
                        )}
                      </li>
                    ))}
                  </ol>
                )}
              </section>
            )}
          </aside>
        </div>
      )}
    </div>
  );
}

function InviteAcceptForm({
  busy,
  onAccept,
}: {
  busy: boolean;
  onAccept: (token: string) => Promise<void>;
}) {
  return (
    <form
      className="setup-form"
      onSubmit={(e) => {
        e.preventDefault();
        const fd = new FormData(e.currentTarget);
        void onAccept(String(fd.get("token") ?? ""));
      }}
    >
      <label className="field">
        <span className="field-label">Have an invite? Paste the token</span>
        <input name="token" type="text" required autoComplete="off" />
      </label>
      <button
        type="submit"
        className="btn btn-secondary"
        disabled={busy}
        aria-busy={busy}
      >
        Accept invitation
      </button>
    </form>
  );
}
