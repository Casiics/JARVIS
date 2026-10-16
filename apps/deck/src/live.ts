// Live task state for the agents view: orchestrator snapshots plus telemetry, merged per task.
// Everything here is metadata (ids, domains, states, durations, error classes), never content.
import { useEffect, useMemo, useState } from "react";
import { getTasks } from "./api";
import type { Domain, OrchestratorTask, TelemetryEvent } from "./schemas";
import { useTelemetry, type LinkState } from "./telemetry";

export interface LiveTask {
  task_id: string;
  domain: Domain;
  kind: string | null;
  state: TelemetryEvent["state"];
  node: string | null;
  updated_at: string;
  duration_ms: number;
  cost_eur: number;
  error_class: string | null;
}

export const ACTIVE_STATES: ReadonlySet<LiveTask["state"]> = new Set([
  "planned",
  "running",
  "gate_check",
  "supervisor_qa",
  "executing",
]);

// The loop's stages in order, for the progress stepper.
export const STAGES = ["planned", "running", "gate_check", "supervisor_qa", "done"] as const;

export function stageIndex(state: LiveTask["state"]): number {
  switch (state) {
    case "planned":
      return 0;
    case "running":
      return 1;
    case "gate_check":
      return 2;
    case "supervisor_qa":
      return 3;
    case "awaiting_action":
    case "executing":
      return 3;
    case "done":
    case "escalated":
      return 4;
  }
}

const isFinal = (state: LiveTask["state"]): boolean => state === "done" || state === "escalated";

const POLL_MS = 15_000;
const MAX_TASKS = 100;

export function fromSnapshot(task: OrchestratorTask): LiveTask {
  return {
    task_id: task.task_id,
    domain: task.domain,
    kind: task.kind,
    state: task.state,
    node: null,
    updated_at: task.updated_at,
    duration_ms: task.duration_ms ?? 0,
    cost_eur: 0,
    error_class: task.error_class,
  };
}

/** Newer information wins; a final state is never undone by an older or late event. */
export function mergeLive(
  current: ReadonlyMap<string, LiveTask>,
  incoming: readonly LiveTask[],
): Map<string, LiveTask> {
  const next = new Map(current);
  for (const task of incoming) {
    const known = next.get(task.task_id);
    if (!known) {
      next.set(task.task_id, task);
      continue;
    }
    const older = Date.parse(task.updated_at) < Date.parse(known.updated_at);
    if (isFinal(known.state) && !isFinal(task.state)) continue;
    // An older event only counts when it is the final one we have not seen yet.
    if (older && !(isFinal(task.state) && !isFinal(known.state))) continue;
    next.set(task.task_id, {
      ...known,
      ...task,
      kind: task.kind ?? known.kind,
      node: task.node ?? known.node,
      cost_eur: Math.max(task.cost_eur, known.cost_eur),
    });
  }
  return next;
}

export function fromTelemetry(event: TelemetryEvent): LiveTask {
  return {
    task_id: event.task_id,
    domain: event.domain,
    kind: null,
    state: event.state,
    node: event.node,
    updated_at: event.ts,
    duration_ms: event.duration_ms,
    cost_eur: event.cost_eur,
    error_class: event.error_class,
  };
}

export interface DomainHealth {
  domain: Domain;
  active: number;
  waiting: number;
  escalated: number;
  done: number;
}

export const DOMAINS: readonly Domain[] = ["core", "trading", "content", "private"];

export function summarise(tasks: readonly LiveTask[]): DomainHealth[] {
  return DOMAINS.map((domain) => {
    const own = tasks.filter((t) => t.domain === domain);
    return {
      domain,
      active: own.filter((t) => ACTIVE_STATES.has(t.state)).length,
      waiting: own.filter((t) => t.state === "awaiting_action").length,
      escalated: own.filter((t) => t.state === "escalated").length,
      done: own.filter((t) => t.state === "done").length,
    };
  });
}

/** Sorted for the lanes: what needs a look first, then what is moving, then the rest. */
export function ordered(tasks: Iterable<LiveTask>): LiveTask[] {
  const rank = (t: LiveTask): number =>
    t.state === "escalated" ? 0 : t.state === "awaiting_action" ? 1 : ACTIVE_STATES.has(t.state) ? 2 : 3;
  return [...tasks]
    .sort((a, b) => rank(a) - rank(b) || Date.parse(b.updated_at) - Date.parse(a.updated_at))
    .slice(0, MAX_TASKS);
}

// What an escalation's error class means for the owner, in their words.
export const ERROR_HINT: Record<string, string> = {
  timeout: "Die Domäne hat sich zu lange nicht gemeldet. Läuft der Worker?",
  max_iterations: "Vier Versuche ohne brauchbares Ergebnis.",
  budget_exhausted: "Das Budget der Aufgabe ist aufgebraucht.",
  model_unavailable: "Das Modell war nicht erreichbar (Ollama läuft?).",
  routing_denied: "Die Routing-Regeln haben den Modellaufruf blockiert.",
  token_expired: "Das Aufgaben-Token ist abgelaufen.",
  tool_not_allowed: "Ein Schritt war vom Aufgaben-Token nicht gedeckt.",
  internal_error: "Der Worker ist mit einem internen Fehler abgebrochen.",
  fetch_failed: "Das Reel konnte nicht geladen werden.",
};

export function errorHint(errorClass: string | null): string {
  if (errorClass === null) return "";
  return ERROR_HINT[errorClass] ?? "Fehler in der Domäne, Details im Worker-Log.";
}

export function useLiveTasks(): {
  tasks: LiveTask[];
  link: LinkState;
  snapshotError: string | null;
} {
  const { tasks: events, link } = useTelemetry();
  const [byId, setById] = useState<Map<string, LiveTask>>(new Map());
  const [snapshotError, setSnapshotError] = useState<string | null>(null);

  useEffect(() => {
    let stopped = false;
    const load = (): void => {
      getTasks()
        .then((list) => {
          if (stopped) return;
          setSnapshotError(null);
          setById((current) => mergeLive(current, list.tasks.map(fromSnapshot)));
        })
        .catch(() => {
          if (!stopped) setSnapshotError("Der Orchestrator ist gerade nicht erreichbar.");
        });
    };
    load();
    const timer = window.setInterval(load, POLL_MS);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, []);

  // Telemetry is newer than the last snapshot in the common case; mergeLive sorts out the rest.
  const tasks = useMemo(
    () => ordered(mergeLive(byId, events.map(fromTelemetry)).values()),
    [byId, events],
  );
  return { tasks, link, snapshotError };
}
