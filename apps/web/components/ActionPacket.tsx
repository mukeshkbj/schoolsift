import type {
  ActionPacket,
  ExecutionRecord,
  ProposalPayload,
  ProposalVersion,
} from "../lib/contracts";
import { headProposals } from "../lib/contracts";
import ProposalReview from "./ProposalReview";

type Props = {
  readOnly?: boolean;
  packet: ActionPacket;
  executions?: ExecutionRecord[];
  localMode?: boolean;
  busyKey: string | null;
  conflicts: Record<string, string>;
  onSave: (version: ProposalVersion, payload: ProposalPayload) => Promise<void>;
  onApprove: (version: ProposalVersion) => Promise<void>;
  onReject: (version: ProposalVersion) => Promise<void>;
  onReload: () => Promise<void>;
  onReanalyze?: (messageId: string) => Promise<void>;
  onDispatch?: () => Promise<void>;
  onRun?: (execution: ExecutionRecord) => Promise<void>;
};

const URGENCY_LABEL = { none: "No rush", soon: "Soon", urgent: "Urgent" };

function fmtDate(iso: string | null): string | null {
  if (iso === null) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? iso
    : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export default function ActionPacketView({
  packet,
  executions = [],
  localMode = false,
  busyKey,
  conflicts,
  onSave,
  onApprove,
  onReject,
  onReload,
  onReanalyze,
  onDispatch,
  onRun,
  readOnly = false,
}: Props) {
  const heads = headProposals(packet);
  const executionFor = (proposalId: string) =>
    executions.find((e) => e.proposal_id === proposalId) ?? null;
  const deadline = fmtDate(packet.deadline);

  return (
    <article className="packet">
      <header className="packet-head">
        <p className="packet-sender">{packet.sender}</p>
        <h2 className="packet-subject">{packet.subject}</h2>
        <div className="packet-facts">
          {packet.child !== null && (
            <span className="fact fact-child">For {packet.child}</span>
          )}
          {deadline !== null && (
            <span className="fact fact-deadline">Due {deadline}</span>
          )}
          <span className={`fact fact-urgency fact-${packet.urgency}`}>
            {URGENCY_LABEL[packet.urgency]}
          </span>
          {packet.information_only && (
            <span className="fact fact-info">Information only</span>
          )}
        </div>
        {!readOnly && onReanalyze && (
          <div className="packet-tools">
            <button
              type="button"
              className="btn btn-quiet btn-sm"
              disabled={busyKey === `reanalyze:${packet.source_message_id}`}
              aria-busy={busyKey === `reanalyze:${packet.source_message_id}`}
              onClick={() => onReanalyze(packet.source_message_id)}
            >
              Re-analyze
            </button>
            <span className="packet-tools-note">
              Re-runs the analysis; current proposals are discarded.
            </span>
          </div>
        )}
      </header>

      <p className="packet-summary">{packet.summary}</p>

      {packet.attachments.length > 0 && (
        <div className="packet-attachments">
          <span className="packet-attachments-label">Read:</span>
          {packet.attachments.map((a) => (
            <span
              key={a.name}
              className={`attachment-chip${a.cited ? " is-cited" : ""}`}
              title={a.cited ? "Used as evidence" : undefined}
            >
              {a.name}
            </span>
          ))}
        </div>
      )}

      {packet.evidence.length > 0 && (
        <section className="packet-section" aria-label="Evidence">
          <h3 className="section-title">Evidence</h3>
          <ul className="evidence-list">
            {packet.evidence.map((e) => (
              <li key={`${e.source}:${e.quote}`} className="evidence-item">
                <span className="evidence-source">
                  {e.source === "body" ? "Email" : e.source}
                </span>
                <blockquote className="evidence-quote">{e.quote}</blockquote>
              </li>
            ))}
          </ul>
        </section>
      )}

      {packet.uncertainties.length > 0 && (
        <section className="packet-section" aria-label="Uncertainties">
          <h3 className="section-title">Needs a look</h3>
          <ul className="uncertainty-list">
            {packet.uncertainties.map((u) => (
              <li key={u}>{u}</li>
            ))}
          </ul>
        </section>
      )}

      <section className="packet-section" aria-label="Proposals">
        <h3 className="section-title">Prepared actions</h3>
        {heads.length === 0 ? (
          <p className="packet-none">
            No actions needed — nothing will be sent or scheduled.
          </p>
        ) : (
          heads.map((v) => (
            <ProposalReview
              key={`${v.id}@${v.version}`}
              version={v}
              execution={executionFor(v.id)}
              localMode={localMode}
              busyKey={busyKey}
              conflict={conflicts[`${v.id}:approve`] ?? conflicts[`${v.id}:edit`] ?? conflicts[`${v.id}:reject`] ?? null}
              readOnly={readOnly}
              onSave={onSave}
              onApprove={onApprove}
              onReject={onReject}
              onReload={onReload}
              onDispatch={onDispatch}
              onRun={onRun}
            />
          ))
        )}
      </section>
    </article>
  );
}
