"use client";

import { useCallback, useEffect, useRef, useState } from "react";
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
  reanalyzeMessage,
  retryMessage,
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
import ChildForm from "./ChildForm";
import SettingsDrawer, { type SettingsSection } from "./SettingsDrawer";
import SendersView from "./SendersView";
import AccountsView from "./AccountsView";

type Props = {
  initialBootstrap?: BootstrapResponse | null;
  onNavigate?: (url: string) => void;
};

type Provider = "gmail" | "outlook";

function railSenderParts(raw: string): { name: string; addr: string } {
  const m = /^(.*?)\s*<([^<>]+)>$/.exec(raw);
  if (m === null) return { name: raw, addr: raw };
  return { name: m[1].trim() || m[2], addr: m[2] };
}

const PROVIDER_LABEL: Record<Provider, string> = {
  gmail: "Gmail",
  outlook: "Outlook",
};

const VIEWS = [
  { key: "review", label: "To review" },
  { key: "senders", label: "Senders" },
  { key: "accounts", label: "Accounts" },
] as const;
type View = (typeof VIEWS)[number]["key"];

export default function SchoolSiftApp({
  initialBootstrap = null,
  onNavigate = (url) => window.location.assign(url),
}: Props) {
  const [boot, setBoot] = useState<BootstrapResponse | null>(initialBootstrap);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [view, setView] = useState<View>("review");
  const [loading, setLoading] = useState(initialBootstrap === null);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [pageError, setPageError] = useState<string | null>(null);
  const [connectedNote, setConnectedNote] = useState<string | null>(null);
  const [conflicts, setConflicts] = useState<Record<string, string>>({});
  const [inviteToken, setInviteToken] = useState<string | null>(null);
  const [inviteCopied, setInviteCopied] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [settingsSection, setSettingsSection] =
    useState<SettingsSection | null>(null);
  const menuButtonRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  const refresh = useCallback(async () => {
    try {
      setBoot(await getBootstrap());
      setPageError(null);
    } catch (e) {
      setPageError(
        e instanceof ApiError ? e.message : "Something went wrong.",
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
      setView("accounts");
      params.delete("connected");
      const next = params.size > 0 ? `?${params}` : window.location.pathname;
      window.history.replaceState(null, "", next);
    }
  }, [initialBootstrap, refresh]);

  useEffect(() => {
    if (!menuOpen) return;
    const items = menuRef.current?.querySelectorAll<HTMLElement>(
      '[role="menuitem"]',
    );
    items?.[0]?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setMenuOpen(false);
        menuButtonRef.current?.focus();
        return;
      }
      if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
      e.preventDefault();
      const all = menuRef.current?.querySelectorAll<HTMLElement>(
        '[role="menuitem"]',
      );
      if (!all || all.length === 0) return;
      const idx = [...all].indexOf(document.activeElement as HTMLElement);
      const next =
        e.key === "ArrowDown"
          ? (idx + 1) % all.length
          : (idx - 1 + all.length) % all.length;
      all[next]?.focus();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [menuOpen]);

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

  const onSource = (sourceId: string, action: "confirm" | "reject") =>
    run(
      () =>
        action === "confirm"
          ? confirmSource(sourceId)
          : rejectSource(sourceId),
      `source-${action}:${sourceId}`,
    );

  const onBulkSources = (
    action: "confirm" | "reject",
    ids: string[],
    onProgress: (done: number) => void,
  ) =>
    run(async () => {
      let done = 0;
      for (const id of ids) {
        if (action === "confirm") await confirmSource(id);
        else await rejectSource(id);
        onProgress(++done);
      }
    }, "bulk-sources");

  const onRetry = (messageId: string) =>
    run(() => retryMessage(messageId), `retry:${messageId}`);

  const onReanalyze = (messageId: string) =>
    run(async () => {
      await reanalyzeMessage(messageId);
      setSelectedId(null);
    }, `reanalyze:${messageId}`);

  const onProcess = (messageId: string) =>
    run(async () => {
      const packet = await processMessage(messageId);
      setSelectedId(packet.id);
      setView("review");
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

  const onAddChild = (name: string, school: string, grade: string) =>
    run(() => addChild(name, school, grade), "child");

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

  const openSettings = (section: SettingsSection) => {
    setMenuOpen(false);
    setSettingsSection(section);
  };

  const closeSettings = useCallback(() => {
    setSettingsSection(null);
    menuButtonRef.current?.focus();
  }, []);

  const packets = boot?.packets ?? [];
  const executions = boot?.executions ?? [];
  const connections = boot?.connections ?? [];
  const sources = boot?.sources ?? [];
  const messages = boot?.messages ?? [];
  const suggestedCount = sources.filter((s) => s.status === "suggested").length;
  const confirmedCount = sources.filter((s) => s.status === "confirmed").length;
  const pendingCount = packets.reduce(
    (n, p) =>
      n + headProposals(p).filter((v) => v.status === "proposed").length,
    0,
  );
  const awaitingCount = messages.filter(
    (m) => m.status === "awaiting_agent",
  ).length;
  const failedMessages = messages.filter((m) => m.status === "failed");
  const timeZone = boot?.household?.timezone;

  const receivedAt = new Map(messages.map((m) => [m.id, m.received_at]));
  const pendingOf = (p: (typeof packets)[number]) =>
    headProposals(p).filter((v) => v.status === "proposed").length;
  const packetRank = (p: (typeof packets)[number]) =>
    p.urgency === "urgent" ? 0 : p.urgency === "soon" ? 1 : pendingOf(p) > 0 ? 2 : 3;
  const tsOrMax = (iso: string | null | undefined) => {
    const t = Date.parse(iso ?? "");
    return Number.isNaN(t) ? Number.MAX_SAFE_INTEGER : t;
  };
  const byUrgencyThenDeadline = (
    a: (typeof packets)[number],
    b: (typeof packets)[number],
  ) =>
    packetRank(a) - packetRank(b) ||
    tsOrMax(a.deadline) - tsOrMax(b.deadline) ||
    tsOrMax(receivedAt.get(a.source_message_id)) -
      tsOrMax(receivedAt.get(b.source_message_id));
  const decisionPackets = packets
    .filter((p) => !p.information_only)
    .sort(byUrgencyThenDeadline);
  const infoPackets = packets
    .filter((p) => p.information_only)
    .sort(byUrgencyThenDeadline);
  const selected = packets.find((p) => p.id === selectedId);
  const activeMembership =
    boot?.memberships.find(
      (m) => m.household_id === boot.active_household_id,
    ) ?? null;
  const role = activeMembership?.role ?? "owner";
  const canEdit = role !== "viewer";
  const isOwner = role === "owner";
  const hasHousehold = boot !== null && boot.household !== null;

  const renderRailItem = (p: (typeof packets)[number]) => {
    const pending = pendingOf(p);
    const sender = railSenderParts(p.sender);
    const pill = p.information_only
      ? { label: "Info", tone: "info" }
      : p.urgency === "urgent"
        ? { label: "Urgent", tone: "urgent" }
        : p.urgency === "soon"
          ? { label: "Soon", tone: "soon" }
          : null;
    return (
      <li key={p.id} className="rail-item">
        <span className="sift-mark" aria-hidden="true" />
        <button
          type="button"
          className={`rail-message${p.id === selectedId ? " is-selected" : ""}`}
          onClick={() => setSelectedId(p.id)}
          aria-current={p.id === selectedId ? "true" : undefined}
        >
          <span className="rail-sender" title={sender.addr}>
            {sender.name}
          </span>
          <span className="rail-subject">{p.subject}</span>
          <span className="rail-meta">
            {pill !== null && (
              <span className={`rail-pill rail-pill-${pill.tone}`}>
                {pill.label}
              </span>
            )}
            {pending > 0
              ? `${pending} to decide`
              : p.information_only
                ? null
                : "Decided"}
          </span>
        </button>
      </li>
    );
  };

  const processAwaiting = () =>
    run(async () => {
      for (const m of messages.filter((x) => x.status === "awaiting_agent")) {
        await processMessage(m.id);
      }
    }, "process-all");

  return (
    <div className="app-shell">
      <header className="app-topbar">
        <div className="app-brand">
          <span className="app-wordmark">SchoolSift</span>
          <span className="app-mode">{boot?.mode ?? "local"} mode</span>
        </div>
        {hasHousehold && (
          <div className="app-topbar-actions">
            <nav className="view-nav" aria-label="Views">
              {VIEWS.map((v) => (
                <button
                  key={v.key}
                  type="button"
                  className={`nav-btn${view === v.key ? " is-active" : ""}`}
                  aria-current={view === v.key ? "page" : undefined}
                  onClick={() => setView(v.key)}
                >
                  {v.label}
                  {v.key === "review" && pendingCount > 0
                    ? ` (${pendingCount})`
                    : v.key === "senders" && suggestedCount > 0
                      ? ` (${suggestedCount})`
                      : ""}
                </button>
              ))}
            </nav>
            <div className="settings-menu">
              <button
                type="button"
                ref={menuButtonRef}
                className="btn btn-quiet"
                aria-haspopup="menu"
                aria-expanded={menuOpen}
                onClick={() => setMenuOpen((o) => !o)}
                onKeyDown={(e) => {
                  if (e.key === "ArrowDown") {
                    e.preventDefault();
                    setMenuOpen(true);
                  }
                }}
              >
                Settings ▾
              </button>
              {menuOpen && (
                <div
                  className="settings-menu-list"
                  role="menu"
                  aria-label="Settings"
                  ref={menuRef}
                >
                  {canEdit && (
                    <button
                      type="button"
                      role="menuitem"
                      onClick={() => openSettings("children")}
                    >
                      Add child
                    </button>
                  )}
                  {isOwner && (
                    <button
                      type="button"
                      role="menuitem"
                      onClick={() => openSettings("caregivers")}
                    >
                      Invite caregiver
                    </button>
                  )}
                  <button
                    type="button"
                    role="menuitem"
                    onClick={() => openSettings("household")}
                  >
                    Household
                  </button>
                </div>
              )}
            </div>
          </div>
        )}
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
          Loading your household…
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
          <ChildForm busy={busyKey === "child"} onAdd={onAddChild} />
        </section>
      ) : (
        <>
          {view === "review" && (
            <div className="workspace">
              <nav className="inbox-rail" aria-label="Action packets">
                <h2 className="rail-heading">What needs you</h2>
                {packets.length === 0 ? (
                  <div className="rail-empty">
                    {connections.length === 0 ? (
                      <>
                        <p>Connect Gmail or Outlook to start reading school mail.</p>
                        <button
                          type="button"
                          className="btn btn-secondary"
                          onClick={() => setView("accounts")}
                        >
                          Connect an inbox
                        </button>
                      </>
                    ) : confirmedCount === 0 && suggestedCount > 0 ? (
                      <>
                        <p>
                          Choose which senders are your school —{" "}
                          {suggestedCount} waiting.
                        </p>
                        <button
                          type="button"
                          className="btn btn-secondary"
                          onClick={() => setView("senders")}
                        >
                          Review senders
                        </button>
                      </>
                    ) : awaitingCount > 0 ? (
                      <>
                        <p>{awaitingCount} school messages ready.</p>
                        {boot.capabilities.agent && canEdit ? (
                          <button
                            type="button"
                            className="btn btn-secondary"
                            disabled={busyKey === "process-all"}
                            aria-busy={busyKey === "process-all"}
                            onClick={() => void processAwaiting()}
                          >
                            Process messages
                            {busyKey === "process-all" && (
                              <span className="btn-busy" aria-hidden="true" />
                            )}
                          </button>
                        ) : (
                          <p className="connection-meta">
                            Waiting for agent configuration — set{" "}
                            <code>SCHOOLSIFT_AWS_REGION</code> and{" "}
                            <code>SCHOOLSIFT_BEDROCK_MODEL_ID</code> on the API.
                          </p>
                        )}
                      </>
                    ) : messages.length === 0 && sources.length === 0 ? (
                      <>
                        <p>
                          Connected — press Sync inbox to look for school
                          mail.
                        </p>
                        <button
                          type="button"
                          className="btn btn-secondary"
                          onClick={() => setView("accounts")}
                        >
                          Go to Accounts
                        </button>
                      </>
                    ) : (
                      <p>
                        Nothing to review yet — packets appear after a
                        connected inbox syncs school mail.
                      </p>
                    )}
                  </div>
                ) : (
                  <>
                    {decisionPackets.length > 0 && (
                      <section
                        className="rail-group"
                        aria-label="Needs a decision"
                      >
                        <h3 className="rail-group-title">Needs a decision</h3>
                        <ol className="rail-list">
                          {decisionPackets.map(renderRailItem)}
                        </ol>
                      </section>
                    )}
                    {infoPackets.length > 0 && (
                      <section
                        className="rail-group"
                        aria-label="For your information"
                      >
                        <h3 className="rail-group-title">
                          For your information
                        </h3>
                        <ol className="rail-list">
                          {infoPackets.map(renderRailItem)}
                        </ol>
                      </section>
                    )}
                  </>
                )}
                {failedMessages.length > 0 && (
                  <div className="rail-failed">
                    <h3 className="rail-heading">Couldn&apos;t process</h3>
                    <ul className="failed-list">
                      {failedMessages.map((m) => (
                        <li key={m.id} className="failed-item">
                          <span className="rail-subject">{m.subject}</span>
                          {m.manual_review_reason !== null && (
                            <span className="rail-meta">
                              {m.manual_review_reason}
                            </span>
                          )}
                          {canEdit && (
                            <button
                              type="button"
                              className="btn btn-secondary btn-sm"
                              disabled={busyKey === `retry:${m.id}`}
                              aria-busy={busyKey === `retry:${m.id}`}
                              onClick={() => void onRetry(m.id)}
                            >
                              Retry analysis
                              {busyKey === `retry:${m.id}` && (
                                <span
                                  className="btn-busy"
                                  aria-hidden="true"
                                />
                              )}
                            </button>
                          )}
                        </li>
                      ))}
                    </ul>
                  </div>
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
                    timeZone={timeZone}
                    busyKey={busyKey}
                    conflicts={conflicts}
                    readOnly={!canEdit}
                    onSave={onSave}
                    onApprove={onApprove}
                    onReject={onReject}
                    onReload={refresh}
                    onReanalyze={onReanalyze}
                    onDispatch={onDispatch}
                    onRun={onRun}
                  />
                )}
              </section>
            </div>
          )}

          {view === "senders" && (
            <div className="view-pane view-pane--wide">
              <SendersView
                sources={sources}
                canEdit={canEdit}
                busyKey={busyKey}
                onSource={onSource}
                onBulkSources={onBulkSources}
              />
            </div>
          )}

          {view === "accounts" && (
            <div className="view-pane">
              <AccountsView
                boot={boot}
                canEdit={canEdit}
                busyKey={busyKey}
                connectedNote={connectedNote}
                onConnect={onConnect}
                onSync={onSync}
                onDisconnect={onDisconnect}
                onNotifications={onNotifications}
                onProcess={onProcess}
              />
            </div>
          )}
        </>
      )}

      {settingsSection !== null && boot !== null && (
        <SettingsDrawer
          boot={boot}
          canEdit={canEdit}
          isOwner={isOwner}
          busyKey={busyKey}
          inviteToken={inviteToken}
          inviteCopied={inviteCopied}
          section={settingsSection}
          onClose={closeSettings}
          onAddChild={onAddChild}
          onInvite={onInvite}
          onRevokeInvite={onRevokeInvite}
          onChangeRole={onChangeRole}
          onRemoveMember={onRemoveMember}
          onInviteCopied={() => setInviteCopied(true)}
        />
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
