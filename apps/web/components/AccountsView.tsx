"use client";

import type { BootstrapResponse } from "../lib/contracts";

const PROVIDER_LABEL = {
  gmail: "Gmail",
  outlook: "Outlook",
} as const;
type Provider = keyof typeof PROVIDER_LABEL;

const MISSING_ENV: Record<Provider, string[]> = {
  gmail: ["GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET"],
  outlook: ["OUTLOOK_CLIENT_ID", "OUTLOOK_CLIENT_SECRET"],
};

const MESSAGE_STATUS: Record<string, string> = {
  awaiting_source: "Waiting for sender review",
  awaiting_agent: "Waiting for agent",
  processed: "Processed",
  failed: "Needs manual review",
};

type Props = {
  boot: BootstrapResponse;
  canEdit: boolean;
  busyKey: string | null;
  connectedNote: string | null;
  onConnect: (provider: Provider) => void;
  onSync: (connectionId: string) => Promise<void>;
  onDisconnect: (connectionId: string) => Promise<void>;
  onNotifications: (connectionId: string) => Promise<void>;
  onProcess: (messageId: string) => Promise<void>;
};

export default function AccountsView({
  boot,
  canEdit,
  busyKey,
  connectedNote,
  onConnect,
  onSync,
  onDisconnect,
  onNotifications,
  onProcess,
}: Props) {
  const connections = boot.connections;

  return (
    <div className="accounts-view">
      <h2 className="rail-heading">Accounts</h2>

      {connections.length > 0 ? (
        <ol className="connection-list">
          {connections.map((c) => (
            <li key={c.id} className="connection-item">
              <span className="connection-email">{c.email}</span>
              <span className="connection-meta">
                {PROVIDER_LABEL[c.provider]} · {c.status.replaceAll("_", " ")}
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
                  disabled={!boot.capabilities[c.provider] || busyKey !== null}
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
          No accounts connected. School mail stays untouched until you connect
          one.
        </p>
      )}

      {connectedNote !== null && (
        <p role="status" className="connect-note">
          {connectedNote}
        </p>
      )}

      {(Object.keys(PROVIDER_LABEL) as Provider[]).map((p) => (
        <div key={p} className="connect-row">
          <button
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
          {!boot.capabilities[p] && (
            <div className="connect-row-note">
              <p className="connection-meta">
                {PROVIDER_LABEL[p]} isn&apos;t set up on this server yet.
              </p>
              <details className="connect-details">
                <summary>Server setup details</summary>
                {MISSING_ENV[p].map((v) => (
                  <code key={v}>{v}</code>
                ))}
              </details>
            </div>
          )}
        </div>
      ))}

      {boot.messages.length > 0 && (
        <section className="source-review" aria-label="Inbox">
          <h3 className="source-heading">Inbox</h3>
          <ol className="connection-list">
            {boot.messages.map((m) => (
              <li key={m.id} className="connection-item">
                <span className="connection-email">{m.subject}</span>
                <span className="connection-meta">
                  {m.sender_email} · {MESSAGE_STATUS[m.status] ?? m.status}
                </span>
                {m.status === "failed" && m.manual_review_reason !== null && (
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
    </div>
  );
}
