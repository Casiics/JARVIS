// Gateway client. Every response is parsed with Zod before the UI sees it.
import {
  startAuthentication,
  startRegistration,
  type PublicKeyCredentialCreationOptionsJSON,
} from "@simplewebauthn/browser";
import { z } from "zod";
import {
  ApprovalDetail,
  Attention,
  CeremonyStart,
  DecisionResult,
  Me,
  ReelList,
  ReelSubmitResult,
  RetryResult,
  TaskList,
} from "./schemas";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

const ErrorBody = z.object({ detail: z.string() });

async function request<T>(schema: z.ZodType<T>, path: string, body?: unknown): Promise<T> {
  const init: RequestInit = { credentials: "same-origin" };
  if (body !== undefined) {
    init.method = "POST";
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(body);
  }
  const response = await fetch(path, init);
  const data: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const parsed = ErrorBody.safeParse(data);
    throw new ApiError(response.status, parsed.success ? parsed.data.detail : response.statusText);
  }
  return schema.parse(data);
}

export const getMe = (): Promise<Me> => request(Me, "/api/me");
export const getAttention = (): Promise<Attention> => request(Attention, "/api/attention");
export const getApproval = (id: string): Promise<ApprovalDetail> =>
  request(ApprovalDetail, `/api/approvals/${encodeURIComponent(id)}`);

export const getReels = (): Promise<z.infer<typeof ReelList>> =>
  request(ReelList, "/api/content/reels");
export const submitReel = (url: string): Promise<z.infer<typeof ReelSubmitResult>> =>
  request(ReelSubmitResult, "/api/content/reels", { url });

/** Queue every failed reel again, e.g. after the content worker was fixed. */
export const retryFailedReels = (): Promise<z.infer<typeof RetryResult>> =>
  request(RetryResult, "/api/content/reels/retry", {});

export const getTasks = (): Promise<z.infer<typeof TaskList>> => request(TaskList, "/api/tasks");

const Ok = z.looseObject({});

export async function loginWithPasskey(): Promise<void> {
  const start = await request(CeremonyStart, "/auth/login/options", {});
  const credential = await startAuthentication({
    optionsJSON: start.options,
  });
  await request(Ok, "/auth/login/verify", { ceremony_id: start.ceremony_id, credential });
}

export async function registerPasskey(bootstrapCode: string | null): Promise<void> {
  const start = await request(CeremonyStart, "/auth/register/options", {
    bootstrap_code: bootstrapCode,
  });
  const credential = await startRegistration({
    optionsJSON: start.options as unknown as PublicKeyCredentialCreationOptionsJSON,
  });
  await request(Ok, "/auth/register/verify", { ceremony_id: start.ceremony_id, credential });
}

export async function logout(): Promise<void> {
  await request(Ok, "/auth/logout", {});
}

/** Approve or reject one held action. Every decision is its own passkey ceremony. */
export async function decide(
  approvalId: string,
  decision: "approve" | "reject",
): Promise<z.infer<typeof DecisionResult>> {
  const id = encodeURIComponent(approvalId);
  const start = await request(CeremonyStart, `/api/approvals/${id}/options`, { decision });
  const credential = await startAuthentication({
    optionsJSON: start.options,
  });
  return request(DecisionResult, `/api/approvals/${id}/verify`, {
    ceremony_id: start.ceremony_id,
    credential,
  });
}
