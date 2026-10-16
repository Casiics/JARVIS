// Runtime schemas for everything that crosses the gateway boundary.
// TelemetryEvent mirrors core/jarvis_core/contracts/telemetry.py; change both together.
import { z } from "zod";

export const Domain = z.enum(["core", "trading", "content", "private"]);
export type Domain = z.infer<typeof Domain>;

export const TelemetryState = z.enum([
  "planned",
  "running",
  "gate_check",
  "supervisor_qa",
  "awaiting_action",
  "executing",
  "done",
  "escalated",
]);
export type TelemetryState = z.infer<typeof TelemetryState>;

const slug = z.string().max(80).regex(/^[a-z0-9_.:-]+$/);

export const TelemetryEvent = z.strictObject({
  ts: z.iso.datetime({ offset: true }),
  task_id: z.uuid(),
  domain: Domain,
  node: slug,
  state: TelemetryState,
  duration_ms: z.number().int().nonnegative(),
  cost_eur: z.number().nonnegative(),
  model_class: z.string().max(40).regex(/^[a-z0-9_-]+$/).nullable(),
  error_class: slug.nullable(),
});
export type TelemetryEvent = z.infer<typeof TelemetryEvent>;

export const ApprovalSummary = z.strictObject({
  approval_id: z.uuid(),
  domain: Domain,
  tool: z.string().max(100),
  tier: z.enum(["A", "B", "C", "D"]),
  tainted: z.boolean(),
  gate_outcome: z.enum(["needs_approval", "needs_step_up"]),
  age_s: z.number().int().nonnegative(),
});
export type ApprovalSummary = z.infer<typeof ApprovalSummary>;

export const Attention = z.strictObject({
  approvals: z.array(ApprovalSummary),
  count: z.number().int().nonnegative(),
});
export type Attention = z.infer<typeof Attention>;

export const ApprovalDetail = ApprovalSummary.extend({
  status: z.enum(["pending", "approved", "rejected"]),
  gate_reason: z.string(),
  task_id: z.uuid(),
  request_hash: z.string().regex(/^[0-9a-f]{64}$/),
  // Absent for the private domain: the deck shows its metadata only.
  rationale: z.string().optional(),
  parameters: z.record(z.string(), z.unknown()).optional(),
});
export type ApprovalDetail = z.infer<typeof ApprovalDetail>;

export const Me = z.strictObject({ user: z.string(), expires_at: z.string() });
export type Me = z.infer<typeof Me>;

// WebAuthn options are validated in shape here and fully by the browser's WebAuthn API.
export const CeremonyStart = z.strictObject({
  ceremony_id: z.string().min(16).max(100),
  options: z.looseObject({ challenge: z.string().min(16) }),
});
export type CeremonyStart = z.infer<typeof CeremonyStart>;

export const DecisionResult = z.strictObject({
  approval_id: z.uuid(),
  status: z.enum(["approved", "rejected"]),
});
