import type { ActionPacket, ProposalPayload, ProposalVersion } from "../lib/contracts";
import { headProposals } from "../lib/contracts";
import ProposalReview from "./ProposalReview";

type Props = {
  packet: ActionPacket;
  busyKey: string | null;
  conflicts: Record<string, string>;
  onSave: (version: ProposalVersion, payload: ProposalPayload) => Promise<void>;
  onApprove: (version: ProposalVersion) => Promise<void>;
  onReject: (version: ProposalVersion) => Promise<void>;
  onReload: () => Promise<void>;
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
  busyKey,
  conflicts,
  onSave,
  onApprove,
  onReject,
  onReload,
}: Props) {
  const heads = headProposals(packet);
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
      </header>

      <p className="packet-summary">{packet.summary}</p>

      {packet.evidence.length > 0 && (
        <section className="packet-section" aria-label="Evidence">
          <h3 className="section-title">Evidence</h3>
          <ul className="evidence-list">
            {packet.evidence.map((e) => (
              <li key={`${e.source}:${e.quote}`} className="evidence-item">
                <span className="evidence-source">{e.source}</span>
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
              busyKey={busyKey}
              conflict={conflicts[`${v.id}:approve`] ?? conflicts[`${v.id}:edit`] ?? conflicts[`${v.id}:reject`] ?? null}
              onSave={onSave}
              onApprove={onApprove}
              onReject={onReject}
              onReload={onReload}
            />
          ))
        )}
      </section>
    </article>
  );
}
