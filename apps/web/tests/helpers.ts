import type {
  BootstrapResponse,
  Connection,
  ProposalVersion,
} from "../lib/contracts";

export const replyVersion: ProposalVersion = {
  id: "packet-1-prop-1",
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
  id: "packet-1-prop-2",
  version: 1,
  status: "proposed",
  payload: {
    kind: "escalation",
    reason: "payment",
    detail: "The message requests a payment.",
  },
  payload_hash: "def456",
};

export const household = {
  id: "hh-1",
  name: "Rivera family",
  timezone: "America/Los_Angeles",
  created_at: "2026-09-13T00:00:00Z",
};

export const child = {
  id: "ch-1",
  household_id: "hh-1",
  name: "Maya",
  school: "Maple Grove Elementary",
  grade: "3",
  created_at: "2026-09-13T00:00:00Z",
};

export const gmailConnection: Connection = {
  id: "conn-1",
  household_id: "hh-1",
  provider: "gmail",
  email: "caregiver@example.com",
  status: "connected",
  last_sync_at: null,
  created_at: "2026-09-13T00:00:00Z",
  sync_status: "idle",
  subscription: null,
};

export const suggestedSource = {
  id: "src-1",
  household_id: "hh-1",
  connection_id: "conn-1",
  sender_email: "office@maplegrove.example",
  sender_domain: "maplegrove.example",
  sender_name: "Maple Grove Office",
  message_count: 3,
  status: "suggested" as const,
  first_seen_at: "2026-09-13T00:00:00Z",
  last_seen_at: "2026-09-13T00:00:00Z",
};

export const inboxMessage = {
  id: "msg-1",
  household_id: "hh-1",
  connection_id: "conn-1",
  provider_message_id: "pm-1",
  thread_id: "t-1",
  sender_name: "Maple Grove Office",
  sender_email: "office@maplegrove.example",
  reply_to: "office@maplegrove.example",
  subject: "Permission needed: field trip",
  received_at: "2026-09-13T00:00:00Z",
  source_confirmed: false,
  status: "awaiting_source" as const,
  manual_review_reason: null,
};

export const packet = {
  id: "packet-1",
  source_message_id: "msg-1",
  sender: "Maple Grove Office <office@maplegrove.example>",
  subject: "Permission needed: field trip",
  summary: "Grade 3 field trip; permission form due.",
  child: "Maya",
  deadline: "2026-09-18T00:00:00Z",
  urgency: "soon" as const,
  information_only: false,
  evidence: [{ source: "body", quote: "permission form by Friday, September 18" }],
  uncertainties: [],
  proposals: [replyVersion],
};

const capabilities = { gmail: false, outlook: false, agent: false, aws: false };

export const ownerMembership = {
  household_id: "hh-1",
  user_id: "local-caregiver",
  email: "local@schoolsift.invalid",
  role: "owner" as const,
  created_at: "2026-09-13T00:00:00Z",
};

export const viewerMembership = { ...ownerMembership, role: "viewer" as const };

export const bootstrapEmpty: BootstrapResponse = {
  mode: "local",
  capabilities,
  household: null,
  active_household_id: null,
  memberships: [],
  members: [],
  invitations: [],
  children: [],
  connections: [],
  sources: [],
  messages: [],
  packets: [],
  executions: [],
};

export const bootstrapHouseholdOnly: BootstrapResponse = {
  ...bootstrapEmpty,
  household,
  active_household_id: "hh-1",
  memberships: [ownerMembership],
  members: [ownerMembership],
};

export const bootstrapReady: BootstrapResponse = {
  ...bootstrapEmpty,
  household,
  active_household_id: "hh-1",
  memberships: [ownerMembership],
  members: [ownerMembership],
  children: [child],
};

export const bootstrapConnected: BootstrapResponse = {
  ...bootstrapReady,
  capabilities: { ...capabilities, gmail: true },
  connections: [gmailConnection],
};

export const bootstrapWithSource: BootstrapResponse = {
  ...bootstrapConnected,
  sources: [suggestedSource],
  messages: [inboxMessage],
};

export const awaitingAgentMessage = {
  ...inboxMessage,
  id: "msg-2",
  provider_message_id: "pm-2",
  thread_id: "t-2",
  subject: "Lunch menu update",
  source_confirmed: true,
  status: "awaiting_agent" as const,
};

export const failedMessage = {
  ...inboxMessage,
  id: "msg-3",
  provider_message_id: "pm-3",
  thread_id: "t-3",
  subject: "Newsletter",
  source_confirmed: true,
  status: "failed" as const,
  manual_review_reason:
    "Attachment flyer.bin exceeds the 25 MB limit; review it in your inbox.",
};

export const confirmedSource = {
  ...suggestedSource,
  status: "confirmed" as const,
};

export const bootstrapAwaitingAgent: BootstrapResponse = {
  ...bootstrapConnected,
  capabilities: { ...capabilities, agent: true },
  sources: [confirmedSource],
  messages: [awaitingAgentMessage],
};

export const bootstrapAgentMissing: BootstrapResponse = {
  ...bootstrapAwaitingAgent,
  capabilities: { ...capabilities, agent: false },
};

export const bootstrapWithFailedMessage: BootstrapResponse = {
  ...bootstrapConnected,
  sources: [confirmedSource],
  messages: [failedMessage],
};

export const bootstrapProcessed: BootstrapResponse = {
  ...bootstrapAwaitingAgent,
  messages: [{ ...awaitingAgentMessage, status: "processed" as const }],
  packets: [packet],
};

export const bootstrapWithPacket: BootstrapResponse = {
  ...bootstrapConnected,
  packets: [packet],
};
