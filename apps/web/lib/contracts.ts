export type EvidenceSpan = { source: string; quote: string };

export type ReplyPayload = {
  kind: "reply";
  recipient: string;
  subject: string;
  body: string;
};

export type CalendarPayload = {
  kind: "calendar";
  title: string;
  starts_at: string;
  ends_at: string;
};

export type PdfFormPayload = {
  kind: "pdf_form";
  document_name: string;
  fields: Record<string, string>;
};

export type EscalationPayload = {
  kind: "escalation";
  reason: "payment" | "signature" | "uncertain" | "unsupported_document";
  detail: string;
};

export type ProposalPayload =
  | ReplyPayload
  | CalendarPayload
  | PdfFormPayload
  | EscalationPayload;

export type ProposalStatus =
  | "proposed"
  | "approved"
  | "rejected"
  | "superseded"
  | "completed";

export type ProposalVersion = {
  id: string;
  version: number;
  status: ProposalStatus;
  payload: ProposalPayload;
  payload_hash: string;
};

export type Urgency = "none" | "soon" | "urgent";

export type ActionPacket = {
  id: string;
  source_message_id: string;
  sender: string;
  subject: string;
  summary: string;
  child: string | null;
  deadline: string | null;
  urgency: Urgency;
  information_only: boolean;
  evidence: EvidenceSpan[];
  uncertainties: string[];
  proposals: ProposalVersion[];
};

export type DemoMessage = {
  id: string;
  provider: string;
  account: string;
  thread_id: string;
  sender: { name: string; email: string };
  reply_to: string;
  subject: string;
  received_at: string;
  source: { id: string; confirmed: boolean; domain: string };
  body: string;
  attachments: { id: string; name: string; mime: string; path: string }[];
};

export type DemoOutcome = {
  id: string;
  kind: string;
  label: string;
  proposal_id: string;
  version: number;
  detail: string;
  created_at: string;
};

export type DemoState = {
  label: string;
  messages: DemoMessage[];
  packets: ActionPacket[];
  outcomes: DemoOutcome[];
};

export function headProposals(packet: ActionPacket): ProposalVersion[] {
  const heads = new Map<string, ProposalVersion>();
  for (const v of packet.proposals) heads.set(v.id, v);
  return [...heads.values()];
}
