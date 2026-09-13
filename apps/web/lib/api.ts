import type {
  ActionPacket,
  DemoOutcome,
  DemoState,
  ProposalPayload,
  ProposalVersion,
} from "./contracts";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
  ) {
    super(message);
  }

  get stale(): boolean {
    return this.status === 409;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API}${path}`, {
    headers: { "content-type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    const body = (await res.json().catch(() => null)) as {
      error?: { code?: string; message?: string };
    } | null;
    throw new ApiError(
      res.status,
      body?.error?.code ?? "UNKNOWN_ERROR",
      body?.error?.message ?? `Request failed with status ${res.status}`,
    );
  }
  return (await res.json()) as T;
}

const post = (body?: unknown) => ({
  method: "POST",
  body: body === undefined ? undefined : JSON.stringify(body),
});

export const getDemo = () => request<DemoState>("/v1/demo");

export const resetDemo = () => request<DemoState>("/v1/demo/reset", post());

export const processInbox = (messageIds: string[] | null = null) =>
  request<{ packets: ActionPacket[] }>(
    "/v1/demo/process",
    post({ message_ids: messageIds }),
  );

export const editProposal = (
  proposalId: string,
  expectedVersion: number,
  payload: ProposalPayload,
) =>
  request<ProposalVersion>(
    `/v1/demo/proposals/${proposalId}/versions`,
    post({ expected_version: expectedVersion, payload }),
  );

export const approveProposal = (
  proposalId: string,
  version: number,
  payloadHash: string,
) =>
  request<{ version: ProposalVersion; outcome: DemoOutcome }>(
    `/v1/demo/proposals/${proposalId}/versions/${version}/approve`,
    post({ payload_hash: payloadHash }),
  );

export const rejectProposal = (
  proposalId: string,
  version: number,
  payloadHash: string,
) =>
  request<ProposalVersion>(
    `/v1/demo/proposals/${proposalId}/versions/${version}/reject`,
    post({ payload_hash: payloadHash }),
  );
