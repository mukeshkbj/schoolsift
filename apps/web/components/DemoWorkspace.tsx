"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ApiError,
  approveProposal,
  editProposal,
  getDemo,
  processInbox,
  rejectProposal,
  resetDemo,
} from "../lib/api";
import type {
  ActionPacket,
  DemoState,
  ProposalPayload,
  ProposalVersion,
} from "../lib/contracts";
import { headProposals } from "../lib/contracts";
import ActionPacketView from "./ActionPacket";

type Props = { initialState?: DemoState | null };

export default function DemoWorkspace({ initialState = null }: Props) {
  const [demo, setDemo] = useState<DemoState | null>(initialState);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [loading, setLoading] = useState(initialState === null);
  const [processing, setProcessing] = useState(false);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [pageError, setPageError] = useState<string | null>(null);
  const [conflicts, setConflicts] = useState<Record<string, string>>({});

  const refresh = useCallback(async () => {
    try {
      setDemo(await getDemo());
      setPageError(null);
    } catch (e) {
      setPageError(
        e instanceof ApiError
          ? e.message
          : "Could not reach the demo API on :8000.",
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (initialState === null) void refresh();
  }, [initialState, refresh]);

  const packetsByMessage = useMemo(() => {
    const map = new Map<string, ActionPacket>();
    for (const p of demo?.packets ?? []) map.set(p.source_message_id, p);
    return map;
  }, [demo]);

  const selected = selectedId ? packetsByMessage.get(selectedId) : undefined;
  const selectedMessage = demo?.messages.find((m) => m.id === selectedId);

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

  async function onProcess() {
    setProcessing(true);
    try {
      const { packets } = await processInbox(null);
      await refresh();
      if (packets.length > 0 && selectedId === null) {
        setSelectedId(packets[0].source_message_id);
      }
      setPageError(null);
    } catch (e) {
      setPageError(e instanceof ApiError ? e.message : "Processing failed.");
    } finally {
      setProcessing(false);
    }
  }

  async function onReset() {
    await run(() => resetDemo(), "reset");
    setSelectedId(null);
    setConflicts({});
  }

  const onSave = (v: ProposalVersion, payload: ProposalPayload) =>
    run(
      () => editProposal(v.id, v.version, payload),
      `${v.id}:edit`,
    );

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

  const processed = demo !== null && demo.packets.length > 0;

  return (
    <div className="demo-shell">
      <header className="demo-topbar">
        <div className="demo-brand">
          <span className="demo-wordmark">SchoolSift</span>
          <span className="demo-badge">Demo data</span>
        </div>
        <div className="demo-actions">
          <button
            type="button"
            className="btn btn-primary"
            onClick={onProcess}
            disabled={processing || loading}
            aria-busy={processing}
          >
            Process demo inbox
            {processing && <span className="btn-busy" aria-hidden="true" />}
          </button>
          <button
            type="button"
            className="btn btn-quiet"
            onClick={onReset}
            disabled={busyKey === "reset" || loading}
            aria-busy={busyKey === "reset"}
          >
            Reset demo
            {busyKey === "reset" && (
              <span className="btn-busy" aria-hidden="true" />
            )}
          </button>
        </div>
      </header>

      {pageError !== null && (
        <p role="alert" className="demo-error">
          {pageError}
        </p>
      )}

      {loading ? (
        <p className="demo-status" role="status">
          Loading demo inbox…
        </p>
      ) : !processed ? (
        <div className="demo-empty">
          <p className="demo-empty-title">
            Six synthetic school messages are waiting.
          </p>
          <p>
            Press <strong>Process demo inbox</strong> and SchoolSift will turn
            each one into a reviewable Action Packet — summary, evidence,
            deadline, and proposals you approve or reject. Nothing real is ever
            sent.
          </p>
        </div>
      ) : (
        <div className="workspace">
          <nav className="inbox-rail" aria-label="Demo inbox messages">
            <h2 className="rail-heading">Inbox</h2>
            <ol className="rail-list">
              {demo?.messages.map((m) => {
                const packet = packetsByMessage.get(m.id);
                const pending = packet
                  ? headProposals(packet).filter((v) => v.status === "proposed")
                      .length
                  : 0;
                return (
                  <li key={m.id} className="rail-item">
                    <span className="sift-mark" aria-hidden="true" />
                    <button
                      type="button"
                      className={`rail-message${m.id === selectedId ? " is-selected" : ""}`}
                      onClick={() => setSelectedId(m.id)}
                      aria-current={m.id === selectedId ? "true" : undefined}
                    >
                      <span className="rail-sender">{m.sender.name}</span>
                      <span className="rail-subject">{m.subject}</span>
                      <span className="rail-meta">
                        {packet === undefined
                          ? "Not processed"
                          : pending > 0
                            ? `${pending} proposal${pending === 1 ? "" : "s"} to review`
                            : packet.information_only
                              ? "Information only"
                              : "Decided"}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ol>
          </nav>

          <section className="packet-pane" aria-label="Action packet">
            {selected === undefined ? (
              <div className="packet-placeholder">
                <p>
                  Select a message to see its Action Packet
                  {selectedMessage ? `: ${selectedMessage.subject}` : "."}
                </p>
              </div>
            ) : (
              <ActionPacketView
                packet={selected}
                busyKey={busyKey}
                conflicts={conflicts}
                onSave={onSave}
                onApprove={onApprove}
                onReject={onReject}
                onReload={refresh}
              />
            )}
          </section>

          <aside className="outcome-pane" aria-label="Demo outcomes">
            <h2 className="rail-heading">Demo outbox &amp; calendar</h2>
            {demo !== null && demo.outcomes.length === 0 ? (
              <p className="outcome-empty">
                Approved proposals land here — labeled demo records only, never
                real email or events.
              </p>
            ) : (
              <ol className="outcome-list">
                {demo?.outcomes.map((o) => (
                  <li key={o.id} className="outcome-item">
                    <span className="outcome-label">{o.label}</span>
                    <span className="outcome-detail">{o.detail}</span>
                  </li>
                ))}
              </ol>
            )}
          </aside>
        </div>
      )}
    </div>
  );
}
