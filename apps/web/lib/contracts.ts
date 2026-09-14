import { z } from "zod";

export const replyPayloadSchema = z.object({
  kind: z.literal("reply"),
  recipient: z.string(),
  subject: z.string(),
  body: z.string(),
});

export const calendarPayloadSchema = z.object({
  kind: z.literal("calendar"),
  title: z.string(),
  starts_at: z.string(),
  ends_at: z.string(),
});

export const pdfFormPayloadSchema = z.object({
  kind: z.literal("pdf_form"),
  recipient: z.string(),
  subject: z.string(),
  body: z.string(),
  document_name: z.string(),
  fields: z.record(z.string(), z.string()),
});

export const escalationPayloadSchema = z.object({
  kind: z.literal("escalation"),
  reason: z.enum(["payment", "signature", "uncertain", "unsupported_document"]),
  detail: z.string(),
});

export const proposalPayloadSchema = z.discriminatedUnion("kind", [
  replyPayloadSchema,
  calendarPayloadSchema,
  pdfFormPayloadSchema,
  escalationPayloadSchema,
]);

export const proposalStatusSchema = z.enum([
  "proposed",
  "approved",
  "rejected",
  "superseded",
  "completed",
]);

export const proposalVersionSchema = z.object({
  id: z.string(),
  version: z.number().int(),
  status: proposalStatusSchema,
  payload: proposalPayloadSchema,
  payload_hash: z.string(),
});

export const executionStatusSchema = z.enum([
  "pending_dispatch",
  "queued",
  "executing",
  "completed",
  "failed",
  "delivery_uncertain",
]);

export const executionRecordSchema = z
  .object({
    id: z.string(),
    household_id: z.string(),
    proposal_id: z.string(),
    proposal_version: z.number().int(),
    connection_id: z.string(),
    idempotency_key: z.string(),
    status: executionStatusSchema,
    attempts: z.number().int(),
    provider_operation_id: z.string().nullable(),
    safe_error: z.string().nullable(),
    created_at: z.string(),
    updated_at: z.string(),
  })
  .strict();

export const approvalResponseSchema = z.object({
  proposal: proposalVersionSchema,
  execution: executionRecordSchema.nullable(),
});

export const dispatchResultSchema = z.object({
  dispatched: z.number().int(),
});

export const runRequestSchema = z.object({ confirm: z.literal(true) });

export const actionPacketSchema = z.object({
  id: z.string(),
  source_message_id: z.string(),
  sender: z.string(),
  subject: z.string(),
  summary: z.string(),
  child: z.string().nullable(),
  deadline: z.string().nullable(),
  urgency: z.enum(["none", "soon", "urgent"]),
  information_only: z.boolean(),
  evidence: z.array(z.object({ source: z.string(), quote: z.string() })),
  uncertainties: z.array(z.string()),
  attachments: z
    .array(
      z.object({
        name: z.string(),
        mime: z.string(),
        cited: z.boolean(),
      })
    )
    .default([]),
  proposals: z.array(proposalVersionSchema),
});

export const householdSchema = z.object({
  id: z.string(),
  name: z.string(),
  timezone: z.string(),
  created_at: z.string(),
});

export const childSchema = z.object({
  id: z.string(),
  household_id: z.string(),
  name: z.string(),
  school: z.string(),
  grade: z.string(),
  created_at: z.string(),
});

export const subscriptionPublicSchema = z
  .object({
    provider: z.enum(["gmail", "outlook"]),
    status: z.enum([
      "active",
      "renewal_due",
      "expired",
      "reauthorization_required",
    ]),
    expires_at: z.string().nullable(),
  })
  .strict();

export const connectionSchema = z
  .object({
    id: z.string(),
    household_id: z.string(),
    provider: z.enum(["gmail", "outlook"]),
    email: z.string(),
    status: z.enum([
      "pending",
      "connected",
      "reauthorization_required",
      "disconnected",
    ]),
    last_sync_at: z.string().nullable(),
    created_at: z.string(),
    sync_status: z.enum([
      "idle",
      "syncing",
      "ready",
      "reauthorization_required",
      "failed",
    ]),
    subscription: subscriptionPublicSchema.nullable(),
  })
  .strict();

export const schoolSourceSchema = z
  .object({
    id: z.string(),
    household_id: z.string(),
    connection_id: z.string(),
    sender_email: z.string(),
    sender_domain: z.string(),
    sender_name: z.string().default(""),
    message_count: z.number().int().default(0),
    status: z.enum(["suggested", "confirmed", "rejected"]),
    first_seen_at: z.string(),
    last_seen_at: z.string(),
  })
  .strict();

export const messageSchema = z
  .object({
    id: z.string(),
    household_id: z.string(),
    connection_id: z.string(),
    provider_message_id: z.string(),
    thread_id: z.string(),
    sender_name: z.string(),
    sender_email: z.string(),
    reply_to: z.string(),
    subject: z.string(),
    received_at: z.string(),
    source_confirmed: z.boolean(),
    status: z.enum([
      "awaiting_source",
      "awaiting_agent",
      "processed",
      "failed",
    ]),
    manual_review_reason: z.string().nullable(),
  })
  .strict();

export const syncResultSchema = z.object({
  discovered: z.number().int(),
  imported: z.number().int(),
  awaiting_source_confirmation: z.number().int(),
  failed: z.number().int(),
  next_cursor: z.string().nullable(),
});

export const capabilitiesSchema = z.object({
  gmail: z.boolean(),
  outlook: z.boolean(),
  agent: z.boolean(),
  aws: z.boolean(),
});

export const roleSchema = z.enum(["owner", "editor", "viewer"]);

export const membershipSchema = z
  .object({
    household_id: z.string(),
    user_id: z.string(),
    email: z.string(),
    role: roleSchema,
    created_at: z.string(),
  })
  .strict();

export const invitationSchema = z
  .object({
    id: z.string(),
    household_id: z.string(),
    email: z.string(),
    role: z.enum(["editor", "viewer"]),
    status: z.enum(["pending", "accepted", "revoked", "expired"]),
    expires_at: z.string(),
    created_at: z.string(),
  })
  .strict();

export const invitationCreatedSchema = z.object({
  invitation: invitationSchema,
  token: z.string().min(1),
});

export const acceptResponseSchema = z.object({
  membership: membershipSchema,
});

const optionalText = z.string().optional();

export const tokenResponseSchema = z
  .object({
    id_token: z.string().min(1),
    token_type: z.literal("Bearer"),
    access_token: optionalText,
    refresh_token: optionalText,
    expires_in: z.number().optional(),
  })
  .strict();

export const bootstrapResponseSchema = z.object({
  mode: z.enum(["local", "aws"]),
  capabilities: capabilitiesSchema,
  household: householdSchema.nullable(),
  active_household_id: z.string().nullable(),
  memberships: z.array(membershipSchema),
  members: z.array(membershipSchema),
  invitations: z.array(invitationSchema),
  children: z.array(childSchema),
  connections: z.array(connectionSchema),
  sources: z.array(schoolSourceSchema),
  messages: z.array(messageSchema),
  packets: z.array(actionPacketSchema),
  executions: z.array(executionRecordSchema),
});

export const oauthStartSchema = z.object({
  authorization_url: z.string(),
});

export const apiErrorSchema = z.object({
  error: z.object({ code: z.string(), message: z.string() }),
});

export type ReplyPayload = z.infer<typeof replyPayloadSchema>;
export type CalendarPayload = z.infer<typeof calendarPayloadSchema>;
export type PdfFormPayload = z.infer<typeof pdfFormPayloadSchema>;
export type EscalationPayload = z.infer<typeof escalationPayloadSchema>;
export type ProposalPayload = z.infer<typeof proposalPayloadSchema>;
export type ProposalStatus = z.infer<typeof proposalStatusSchema>;
export type ProposalVersion = z.infer<typeof proposalVersionSchema>;
export type ActionPacket = z.infer<typeof actionPacketSchema>;
export type Household = z.infer<typeof householdSchema>;
export type Child = z.infer<typeof childSchema>;
export type Connection = z.infer<typeof connectionSchema>;
export type SchoolSource = z.infer<typeof schoolSourceSchema>;
export type Message = z.infer<typeof messageSchema>;
export type SyncResult = z.infer<typeof syncResultSchema>;
export type Capabilities = z.infer<typeof capabilitiesSchema>;
export type Role = z.infer<typeof roleSchema>;
export type Membership = z.infer<typeof membershipSchema>;
export type Invitation = z.infer<typeof invitationSchema>;
export type BootstrapResponse = z.infer<typeof bootstrapResponseSchema>;
export type ExecutionStatus = z.infer<typeof executionStatusSchema>;
export type ExecutionRecord = z.infer<typeof executionRecordSchema>;
export type ApprovalResponse = z.infer<typeof approvalResponseSchema>;

export function headProposals(packet: ActionPacket): ProposalVersion[] {
  const heads = new Map<string, ProposalVersion>();
  for (const v of packet.proposals) heads.set(v.id, v);
  return [...heads.values()];
}
