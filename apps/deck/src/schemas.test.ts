import { describe, expect, it } from "vitest";
import {
  ApprovalDetail,
  Attention,
  ContextReviewList,
  TelemetryEvent,
} from "./schemas";
import { mergeEvent, parseTelemetry } from "./telemetry";

// Shapes as the Python gateway serialises them.
const event = {
  ts: "2026-10-16T07:40:00.123456Z",
  task_id: "0b8a3c43-6f0e-4a7d-9a43-3f1f9a2c1e11",
  domain: "content",
  node: "reel.extract",
  state: "running",
  duration_ms: 1200,
  cost_eur: 0,
  model_class: "reasoning",
  error_class: null,
};

describe("telemetry", () => {
  it("accepts gateway events", () => {
    expect(TelemetryEvent.parse(event).node).toBe("reel.extract");
  });

  it("drops events that carry content or unknown fields", () => {
    expect(
      parseTelemetry(JSON.stringify({ ...event, prompt: "secret" })),
    ).toBeNull();
    expect(
      parseTelemetry(JSON.stringify({ ...event, node: "free text" })),
    ).toBeNull();
    expect(parseTelemetry("not json")).toBeNull();
    expect(parseTelemetry(42)).toBeNull();
  });

  it("keeps the latest event per task", () => {
    const first = TelemetryEvent.parse(event);
    const second = TelemetryEvent.parse({ ...event, state: "done" });
    const merged = mergeEvent(mergeEvent([], first), second);
    expect(merged).toHaveLength(1);
    expect(merged[0]?.state).toBe("done");
  });
});

describe("approvals", () => {
  const summary = {
    approval_id: "8f0c8d0e-1b7a-4c5e-9b8e-2f6f3a7d9c10",
    domain: "private",
    tool: "send_email",
    tier: "C",
    tainted: false,
    gate_outcome: "needs_approval",
    age_s: 30,
  };

  it("parses the attention queue", () => {
    expect(Attention.parse({ approvals: [summary], count: 1 }).count).toBe(1);
  });

  it("accepts private approvals without content", () => {
    const detail = ApprovalDetail.parse({
      ...summary,
      status: "pending",
      gate_reason: "tier C needs human approval",
      task_id: "0b8a3c43-6f0e-4a7d-9a43-3f1f9a2c1e11",
      request_hash: "a".repeat(64),
    });
    expect(detail.rationale).toBeUndefined();
  });

  it("rejects unexpected fields", () => {
    expect(
      Attention.safeParse({ approvals: [{ ...summary, raw: "x" }], count: 1 })
        .success,
    ).toBe(false);
  });
});

describe("context reviews", () => {
  const review = {
    brief_id: "br:0123456789abcdef01234567",
    category: "market-notes",
    proposal: "store",
    confidence: 0.67,
    title: "A QQQ opening range breakout",
    summary: "Waits for the first 15 minutes.",
    facts: ["Wait 15 minutes"],
    reasons: ["category market-notes"],
    flags: [],
    source_refs: ["https://www.instagram.com/reel/ABCDE12345/"],
    received_at: "2026-10-16T12:00:00+00:00",
    decided_at: null,
    accepted: null,
  };

  it("parses the gateway listing", () => {
    expect(
      ContextReviewList.parse({ open: [review], recent: [] }).open[0]?.category,
    ).toBe("market-notes");
  });

  it("rejects malformed ids and extra fields", () => {
    expect(
      ContextReviewList.safeParse({
        open: [{ ...review, brief_id: "x" }],
        recent: [],
      }).success,
    ).toBe(false);
    expect(
      ContextReviewList.safeParse({
        open: [{ ...review, html: "<b>" }],
        recent: [],
      }).success,
    ).toBe(false);
  });
});
