import type { DemoState, ProposalVersion } from "../lib/contracts";

export const replyVersion: ProposalVersion = {
  id: "msg-field-trip-prop-1-reply",
  version: 1,
  status: "proposed",
  payload: {
    kind: "reply",
    recipient: "office@maplegrove.example",
    subject: "Re: Field trip",
    body: "Maya will attend.",
  },
  payload_hash: "abc123",
};

export const escalationVersion: ProposalVersion = {
  id: "msg-x-prop-1-escalation",
  version: 1,
  status: "proposed",
  payload: {
    kind: "escalation",
    reason: "payment",
    detail: "The message requests a payment.",
  },
  payload_hash: "def456",
};

export const demoState: DemoState = {
  label: "Demo data",
  messages: [
    {
      id: "msg-field-trip",
      provider: "gmail",
      account: "alex@mail.example",
      thread_id: "t1",
      sender: { name: "Maple Grove Office", email: "office@maplegrove.example" },
      reply_to: "office@maplegrove.example",
      subject: "Permission needed: field trip",
      received_at: "2026-09-10T08:14:00Z",
      source: { id: "src-maplegrove", confirmed: true, domain: "maplegrove.example" },
      body: "See subject.",
      attachments: [],
    },
  ],
  packets: [
    {
      id: "packet-msg-field-trip",
      source_message_id: "msg-field-trip",
      sender: "Maple Grove Office <office@maplegrove.example>",
      subject: "Permission needed: field trip",
      summary: "Grade 3 field trip; permission form due.",
      child: "Maya",
      deadline: "2026-09-18T00:00:00Z",
      urgency: "soon",
      information_only: false,
      evidence: [{ source: "body", quote: "permission form by Friday, September 18" }],
      uncertainties: [],
      proposals: [replyVersion],
    },
  ],
  outcomes: [],
};
