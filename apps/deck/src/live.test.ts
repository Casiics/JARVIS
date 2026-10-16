import { describe, expect, it } from "vitest";
import {
  costToday,
  errorHint,
  fromSnapshot,
  fromTelemetry,
  mergeLive,
  ordered,
  recentEscalations,
  stageIndex,
  summarise,
  type LiveTask,
} from "./live";
import { OrchestratorTask, TaskList } from "./schemas";

const ID = "0b8a3c43-6f0e-4a7d-9a43-3f1f9a2c1e11";

// As core/jarvis_core/api/app.py `_task` serialises a record.
const snapshot = {
  task_id: ID,
  kind: "reel",
  domain: "content",
  state: "running",
  created_at: "2026-10-16T12:00:00+00:00",
  updated_at: "2026-10-16T12:00:05+00:00",
  duration_ms: 5000,
  error_class: null,
  cost_eur: 0.0123,
} as const;

function task(over: Partial<LiveTask>): LiveTask {
  return { ...fromSnapshot(OrchestratorTask.parse(snapshot)), ...over };
}

describe("orchestrator task schema", () => {
  it("accepts the gateway's task list", () => {
    expect(TaskList.parse({ tasks: [snapshot] }).tasks[0]?.kind).toBe("reel");
  });

  it("rejects tasks that carry the goal or other content", () => {
    expect(OrchestratorTask.safeParse({ ...snapshot, source_ref: "https://x" }).success).toBe(false);
    expect(OrchestratorTask.safeParse({ ...snapshot, domain: "elsewhere" }).success).toBe(false);
  });
});

describe("mergeLive", () => {
  it("lets newer information win and keeps what only one source knows", () => {
    const first = mergeLive(new Map(), [task({})]);
    const next = mergeLive(first, [
      fromTelemetry({
        ts: "2026-10-16T12:00:09Z",
        task_id: ID,
        domain: "content",
        node: "loop:gate",
        state: "gate_check",
        duration_ms: 9000,
        cost_eur: 0.002,
        model_class: null,
        error_class: null,
      }),
    ]);
    const merged = next.get(ID);
    expect(merged?.state).toBe("gate_check");
    expect(merged?.kind).toBe("reel"); // from the snapshot
    expect(merged?.node).toBe("loop:gate"); // from telemetry
  });

  it("never reopens a finished task", () => {
    const done = mergeLive(new Map(), [task({ state: "done", updated_at: "2026-10-16T12:01:00Z" })]);
    const late = mergeLive(done, [task({ state: "running", updated_at: "2026-10-16T12:02:00Z" })]);
    expect(late.get(ID)?.state).toBe("done");
  });

  it("ignores stale updates but accepts a final state it had missed", () => {
    const now = mergeLive(new Map(), [task({ state: "gate_check", updated_at: "2026-10-16T12:05:00Z" })]);
    expect(mergeLive(now, [task({ state: "running" })]).get(ID)?.state).toBe("gate_check");
    expect(mergeLive(now, [task({ state: "escalated" })]).get(ID)?.state).toBe("escalated");
  });
});

describe("summaries", () => {
  it("counts per domain and sorts escalations first", () => {
    const tasks = [
      task({ task_id: "a", state: "running" }),
      task({ task_id: "b", state: "escalated", domain: "trading", error_class: "timeout" }),
      task({ task_id: "c", state: "done" }),
      task({ task_id: "d", state: "awaiting_action", domain: "private" }),
    ];
    const health = summarise(tasks);
    expect(health.find((h) => h.domain === "content")).toMatchObject({ active: 1, done: 1 });
    expect(health.find((h) => h.domain === "trading")?.escalated).toBe(1);
    expect(health.find((h) => h.domain === "private")?.waiting).toBe(1);
    expect(ordered(tasks).map((t) => t.task_id)).toEqual(["b", "d", "a", "c"]);
  });

  it("maps states onto the loop's stages", () => {
    expect(stageIndex("planned")).toBe(0);
    expect(stageIndex("supervisor_qa")).toBe(3);
    expect(stageIndex("done")).toBe(4);
  });

  it("explains escalations in plain words", () => {
    expect(errorHint("timeout")).toContain("Worker");
    expect(errorHint("something_new")).toContain("Worker-Log");
    expect(errorHint(null)).toBe("");
  });
});

describe("recentEscalations", () => {
  it("counts only escalations from the last day", () => {
    const now = Date.parse("2026-10-16T12:00:00Z");
    const base = OrchestratorTask.parse(snapshot);
    const tasks = [
      { ...base, state: "escalated" as const, updated_at: "2026-10-16T11:00:00+00:00" },
      { ...base, state: "escalated" as const, updated_at: "2026-10-14T11:00:00+00:00" },
      { ...base, state: "done" as const },
    ];
    expect(recentEscalations(tasks, now)).toBe(1);
  });
});

describe("costToday", () => {
  it("adds up today's spend and keeps what the snapshot reported", () => {
    const now = new Date("2026-10-16T12:00:00");
    const today = task({ task_id: "a", updated_at: new Date("2026-10-16T09:00:00").toISOString() });
    const old = task({ task_id: "b", updated_at: new Date("2026-10-15T09:00:00").toISOString() });
    expect(today.cost_eur).toBe(0.0123);
    expect(costToday([today, old], now)).toBeCloseTo(0.0123);
  });
});
