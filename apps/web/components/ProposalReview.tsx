"use client";

import { useState } from "react";
import type {
  CalendarPayload,
  ExecutionRecord,
  PdfFormPayload,
  ProposalPayload,
  ProposalVersion,
  ReplyPayload,
} from "../lib/contracts";
import { formatRangeInZone } from "../lib/dates";

type Props = {
  readOnly?: boolean;
  version: ProposalVersion;
  execution?: ExecutionRecord | null;
  localMode?: boolean;
  timeZone?: string;
  busyKey: string | null;
  conflict: string | null;
  onSave: (version: ProposalVersion, payload: ProposalPayload) => Promise<void>;
  onApprove: (version: ProposalVersion) => Promise<void>;
  onReject: (version: ProposalVersion) => Promise<void>;
  onReload: () => Promise<void>;
  onDispatch?: () => Promise<void>;
  onRun?: (execution: ExecutionRecord) => Promise<void>;
};

const APPROVE_LABEL: Record<string, string> = {
  reply: "Approve reply",
  calendar: "Approve calendar event",
  pdf_form: "Approve completed form",
};

const KIND_LABEL: Record<string, string> = {
  reply: "Draft reply",
  calendar: "Calendar event",
  pdf_form: "Fillable form",
  escalation: "Escalation",
};

const DIRTY_NOTE: Record<string, string> = {
  reply: "Save your edits before sending.",
  calendar: "Save your edits before adding to the calendar.",
  pdf_form: "Save your edits before completing the form.",
};

const EXECUTION_LABEL: Record<string, string> = {
  pending_dispatch: "Pending — approved but not yet dispatched.",
  queued: "Queued — ready to run against the connected account.",
  executing: "Executing against the connected account.",
  completed: "Completed — the provider accepted the action.",
  failed: "Failed — the action did not complete.",
  delivery_uncertain:
    "Delivery uncertain — the provider may have completed it; it will not be retried automatically.",
};

function approvalConsequence(
  version: ProposalVersion,
  localMode: boolean,
): string {
  const payload = version.payload;
  if (localMode) {
    if (payload.kind === "reply") {
      return `Approving queues this email to ${payload.recipient}; nothing is sent until you run the approved action.`;
    }
    if (payload.kind === "calendar") {
      return "Approving queues this calendar event; nothing is created until you run the approved action.";
    }
    if (payload.kind === "pdf_form") {
      return `Approving queues filling ${payload.document_name} and emailing it to ${payload.recipient}; nothing is sent until you run the approved action.`;
    }
    return "Approving queues this action; nothing happens until you run it.";
  }
  if (payload.kind === "reply") {
    return `Approving will send this email to ${payload.recipient}.`;
  }
  if (payload.kind === "calendar") {
    return "Approving will create this event on your connected calendar.";
  }
  if (payload.kind === "pdf_form") {
    return `Approving will fill ${payload.document_name} and email it to ${payload.recipient}.`;
  }
  return "Approving will perform this action.";
}

function runConsequence(version: ProposalVersion): string {
  const payload = version.payload;
  if (payload.kind === "calendar") {
    return "create this calendar event";
  }
  if (payload.kind === "reply" || payload.kind === "pdf_form") {
    return `send this email to ${payload.recipient}`;
  }
  return "perform this action";
}

function toLocalInput(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function fromLocalInput(value: string): string {
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? value : d.toISOString();
}

export default function ProposalReview({
  version,
  execution = null,
  localMode = false,
  timeZone,
  busyKey,
  conflict,
  onSave,
  onApprove,
  onReject,
  onReload,
  onDispatch,
  onRun,
  readOnly = false,
}: Props) {
  const [draft, setDraft] = useState<ProposalPayload>(version.payload);
  const [confirmRun, setConfirmRun] = useState(false);

  const dirty = JSON.stringify(draft) !== JSON.stringify(version.payload);

  const proposed = version.status === "proposed";
  const escalation = version.payload.kind === "escalation";
  const approveLabel = APPROVE_LABEL[version.payload.kind];

  const busy =
    busyKey !== null && busyKey.startsWith(`${version.id}:`);

  function patch(part: Partial<ProposalPayload>) {
    setDraft((d) => ({ ...d, ...part }) as ProposalPayload);
  }

  async function save() {
    await onSave(version, draft);
  }

  return (
    <section
      className={`proposal proposal-${version.payload.kind}`}
      aria-label={`${KIND_LABEL[version.payload.kind]} proposal, version ${version.version}`}
    >
      <header className="proposal-head">
        <span className="proposal-kind">{KIND_LABEL[version.payload.kind]}</span>
        <span className={`proposal-status status-${version.status}`}>
          {version.status}
          {version.version > 1 ? ` · v${version.version}` : ""}
        </span>
      </header>

      {version.payload.kind === "reply" && draft.kind === "reply" && (
        <div className="proposal-fields">
          <p className="field-note">
            Replies can only go to the school thread:{" "}
            <strong>{version.payload.recipient}</strong>
          </p>
          <label className="field">
            <span className="field-label">Subject</span>
            <input
              type="text"
              value={(draft as ReplyPayload).subject}
              disabled={!proposed || readOnly}
              onChange={(e) =>
                patch({ subject: e.target.value } as Partial<ReplyPayload>)
              }
            />
          </label>
          <label className="field">
            <span className="field-label">Body</span>
            <textarea
              rows={5}
              value={(draft as ReplyPayload).body}
              disabled={!proposed || readOnly}
              onChange={(e) =>
                patch({ body: e.target.value } as Partial<ReplyPayload>)
              }
            />
          </label>
        </div>
      )}

      {version.payload.kind === "calendar" && draft.kind === "calendar" && (
        <div className="proposal-fields">
          <label className="field">
            <span className="field-label">Title</span>
            <input
              type="text"
              value={(draft as CalendarPayload).title}
              disabled={!proposed || readOnly}
              onChange={(e) =>
                patch({ title: e.target.value } as Partial<CalendarPayload>)
              }
            />
          </label>
          <div className="field-row">
            <label className="field">
              <span className="field-label">Starts</span>
              <input
                type="datetime-local"
                value={toLocalInput((draft as CalendarPayload).starts_at)}
                disabled={!proposed || readOnly}
                onChange={(e) =>
                  patch({
                    starts_at: fromLocalInput(e.target.value),
                  } as Partial<CalendarPayload>)
                }
              />
            </label>
            <label className="field">
              <span className="field-label">Ends</span>
              <input
                type="datetime-local"
                value={toLocalInput((draft as CalendarPayload).ends_at)}
                disabled={!proposed || readOnly}
                onChange={(e) =>
                  patch({
                    ends_at: fromLocalInput(e.target.value),
                  } as Partial<CalendarPayload>)
                }
              />
            </label>
          </div>
          {formatRangeInZone(
            (draft as CalendarPayload).starts_at,
            (draft as CalendarPayload).ends_at,
            timeZone,
          ) !== null && (
            <p className="field-note">
              In your household time zone:{" "}
              {formatRangeInZone(
                (draft as CalendarPayload).starts_at,
                (draft as CalendarPayload).ends_at,
                timeZone,
              )}
            </p>
          )}
        </div>
      )}

      {version.payload.kind === "pdf_form" && draft.kind === "pdf_form" && (
        <div className="proposal-fields">
          <p className="field-note">
            The completed form is emailed to the school thread:{" "}
            <strong>{version.payload.recipient}</strong>
          </p>
          <p className="field-note">
            Form: <strong>{version.payload.document_name}</strong>
          </p>
          <label className="field">
            <span className="field-label">Subject</span>
            <input
              type="text"
              value={(draft as PdfFormPayload).subject}
              disabled={!proposed || readOnly}
              onChange={(e) =>
                patch({ subject: e.target.value } as Partial<PdfFormPayload>)
              }
            />
          </label>
          <label className="field">
            <span className="field-label">Body</span>
            <textarea
              rows={3}
              value={(draft as PdfFormPayload).body}
              disabled={!proposed || readOnly}
              onChange={(e) =>
                patch({ body: e.target.value } as Partial<PdfFormPayload>)
              }
            />
          </label>
          {Object.entries((draft as PdfFormPayload).fields).map(
            ([name, value]) => (
              <label className="field" key={name}>
                <span className="field-label">{name.replaceAll("_", " ")}</span>
                <input
                  type="text"
                  value={value}
                  disabled={!proposed || readOnly}
                  onChange={(e) =>
                    patch({
                      fields: {
                        ...(draft as PdfFormPayload).fields,
                        [name]: e.target.value,
                      },
                    } as Partial<PdfFormPayload>)
                  }
                />
              </label>
            ),
          )}
        </div>
      )}

      {version.payload.kind === "escalation" && (
        <div className="proposal-fields">
          <p className="escalation-reason">Reason: {version.payload.reason}</p>
          <p>{version.payload.detail}</p>
          <p className="field-note">
            Escalations are for a human to handle — SchoolSift can never approve
            or execute them.
          </p>
        </div>
      )}

      {conflict !== null && (
        <p role="alert" className="proposal-conflict">
          {conflict}{" "}
          <button type="button" className="btn btn-quiet" onClick={onReload}>
            Reload latest
          </button>
        </p>
      )}

      <footer className="proposal-actions">
        {proposed && !escalation && (
          <>
            <button
              type="button"
              className="btn btn-primary"
              disabled={busy || dirty || readOnly}
              aria-busy={busy && busyKey === `${version.id}:approve`}
              onClick={() => onApprove(version)}
            >
              {approveLabel}
              {busy && busyKey === `${version.id}:approve` && (
                <span className="btn-busy" aria-hidden="true" />
              )}
            </button>
            {dirty && (
              <p className="dirty-note">{DIRTY_NOTE[version.payload.kind]}</p>
            )}
            <p className="field-note approve-consequence">
              {approvalConsequence(version, localMode)}
            </p>
          </>
        )}
        {proposed && escalation && (
          <span className="escalation-noapprove" aria-label="Approval unavailable">
            Approval unavailable for escalations
          </span>
        )}
        {proposed && (
          <>
            {!escalation && (
              <button
                type="button"
                className="btn btn-secondary"
                disabled={busy || !dirty || readOnly}
                aria-busy={busy && busyKey === `${version.id}:edit`}
                onClick={save}
              >
                Save edit as new version
                {busy && busyKey === `${version.id}:edit` && (
                  <span className="btn-busy" aria-hidden="true" />
                )}
              </button>
            )}
            <button
              type="button"
              className="btn btn-danger"
              disabled={busy || readOnly}
              aria-busy={busy && busyKey === `${version.id}:reject`}
              onClick={() => onReject(version)}
            >
              Reject proposal
              {busy && busyKey === `${version.id}:reject` && (
                <span className="btn-busy" aria-hidden="true" />
              )}
            </button>
          </>
        )}
        {version.status === "approved" && (
          <div className="execution-panel">
            <p
              className={`proposal-done execution-${execution?.status ?? "pending_dispatch"}`}
            >
              {execution === null
                ? "Approved — queued for delivery."
                : EXECUTION_LABEL[execution.status]}
            </p>
            {execution?.safe_error && (
              <p className="execution-error" role="alert">
                {execution.safe_error}
              </p>
            )}
            {localMode &&
              !readOnly &&
              execution !== null &&
              (execution.status === "pending_dispatch" ||
                execution.status === "queued") && (
                <div className="execution-controls">
                  {execution.status === "pending_dispatch" &&
                    onDispatch !== undefined && (
                      <button
                        type="button"
                        className="btn btn-secondary"
                        disabled={busy}
                        onClick={() => onDispatch()}
                      >
                        Dispatch
                      </button>
                    )}
                  {onRun !== undefined && (
                    <>
                      <label className="field execution-confirm">
                        <input
                          type="checkbox"
                          checked={confirmRun}
                          onChange={(e) => setConfirmRun(e.target.checked)}
                        />
                        <span>
                          I understand this will {runConsequence(version)} on the
                          connected account.
                        </span>
                      </label>
                      <button
                        type="button"
                        className="btn btn-primary"
                        disabled={busy || !confirmRun}
                        aria-busy={
                          busy && busyKey === `${version.id}:run`
                        }
                        onClick={() => {
                          setConfirmRun(false);
                          void onRun(execution);
                        }}
                      >
                        Run approved action
                      </button>
                    </>
                  )}
                </div>
              )}
          </div>
        )}
        {version.status === "completed" && (
          <p className="proposal-done">Completed.</p>
        )}
        {version.status === "rejected" && (
          <p className="proposal-done">Rejected — this decision is final.</p>
        )}
      </footer>
    </section>
  );
}
