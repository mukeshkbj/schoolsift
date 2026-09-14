import { z } from "zod";
import { getActiveHousehold, getIdToken } from "./auth";
import { ApiError } from "./errors";
import {
  acceptResponseSchema,
  actionPacketSchema,
  apiErrorSchema,
  approvalResponseSchema,
  bootstrapResponseSchema,
  dispatchResultSchema,
  childSchema,
  executionRecordSchema,
  householdSchema,
  invitationCreatedSchema,
  invitationSchema,
  membershipSchema,
  oauthStartSchema,
  proposalPayloadSchema,
  proposalVersionSchema,
  runRequestSchema,
  schoolSourceSchema,
  subscriptionPublicSchema,
  syncResultSchema,
} from "./contracts";
import type { ProposalPayload } from "./contracts";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";

function requestHeaders(): Record<string, string> {
  const headers: Record<string, string> = {
    "content-type": "application/json",
  };
  const token = getIdToken();
  if (token !== null) headers.authorization = `Bearer ${token}`;
  const household = getActiveHousehold();
  if (household !== null) headers["x-schoolsift-household"] = household;
  return headers;
}

async function request<S extends z.ZodType>(
  path: string,
  schema: S,
  init?: RequestInit,
): Promise<z.infer<S>> {
  const res = await fetch(`${API}${path}`, {
    ...init,
    headers: requestHeaders(),
  });
  const body: unknown =
    res.status === 204 ? null : await res.json().catch(() => null);
  if (!res.ok) {
    const parsed = apiErrorSchema.safeParse(body);
    throw new ApiError(
      res.status,
      parsed.success ? parsed.data.error.code : "UNKNOWN_ERROR",
      parsed.success
        ? parsed.data.error.message
        : `Request failed with status ${res.status}`,
    );
  }
  return schema.parse(body);
}

const post = (body?: unknown) => ({
  method: "POST",
  body: body === undefined ? undefined : JSON.stringify(body),
});

export const getBootstrap = () =>
  request("/v1/bootstrap", bootstrapResponseSchema);

export const createHousehold = (name: string, timezone: string) =>
  request("/v1/household", householdSchema, post({ name, timezone }));

export const addChild = (name: string, school: string, grade: string) =>
  request("/v1/children", childSchema, post({ name, school, grade }));

export const startConnect = (provider: "gmail" | "outlook") =>
  request(`/v1/connections/${provider}/authorize`, oauthStartSchema, post());

export const disconnectConnection = (connectionId: string) =>
  request(`/v1/connections/${connectionId}`, z.null(), {
    method: "DELETE",
  });

export const syncConnection = (connectionId: string) =>
  request(`/v1/connections/${connectionId}/sync`, syncResultSchema, post());

export const enableNotifications = (connectionId: string) =>
  request(
    `/v1/connections/${connectionId}/notifications`,
    subscriptionPublicSchema,
    post(),
  );

export const confirmSource = (sourceId: string) =>
  request(`/v1/sources/${sourceId}/confirm`, schoolSourceSchema, post());

export const rejectSource = (sourceId: string) =>
  request(`/v1/sources/${sourceId}/reject`, schoolSourceSchema, post());

export const processMessage = (messageId: string) =>
  request(`/v1/messages/${messageId}/process`, actionPacketSchema, post());

export const editProposal = (
  proposalId: string,
  expectedVersion: number,
  payload: ProposalPayload,
) =>
  request(
    `/v1/proposals/${proposalId}/versions`,
    proposalVersionSchema,
    post({
      expected_version: expectedVersion,
      payload: proposalPayloadSchema.parse(payload),
    }),
  );

export const approveProposal = (
  proposalId: string,
  version: number,
  payloadHash: string,
) =>
  request(
    `/v1/proposals/${proposalId}/versions/${version}/approve`,
    approvalResponseSchema,
    post({ payload_hash: payloadHash }),
  );

export const listMembers = () =>
  request("/v1/members", z.array(membershipSchema));

export const listInvitations = () =>
  request("/v1/invitations", z.array(invitationSchema));

export const createInvitation = (
  email: string,
  role: "editor" | "viewer",
) =>
  request("/v1/invitations", invitationCreatedSchema, post({ email, role }));

export const revokeInvitation = (invitationId: string) =>
  request(`/v1/invitations/${invitationId}`, z.null(), { method: "DELETE" });

export const acceptInvitation = (token: string) =>
  request("/v1/invitations/accept", acceptResponseSchema, post({ token }));

export const changeMemberRole = (userId: string, role: string) =>
  request(`/v1/members/${userId}`, membershipSchema, {
    method: "PATCH",
    body: JSON.stringify({ role }),
  });

export const removeMember = (userId: string) =>
  request(`/v1/members/${userId}`, z.null(), { method: "DELETE" });

export const rejectProposal = (
  proposalId: string,
  version: number,
  payloadHash: string,
) =>
  request(
    `/v1/proposals/${proposalId}/versions/${version}/reject`,
    proposalVersionSchema,
    post({ payload_hash: payloadHash }),
  );

export const listExecutions = () =>
  request("/v1/executions", z.array(executionRecordSchema));

export const dispatchExecutions = () =>
  request("/v1/executions/dispatch", dispatchResultSchema, post());

export const runExecution = (executionId: string) =>
  request(
    `/v1/executions/${executionId}/run`,
    executionRecordSchema,
    post(runRequestSchema.parse({ confirm: true })),
  );
