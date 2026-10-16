import { useState } from "react";
import { ApiError, retryFailedReels } from "../api";
import { DOMAIN_LABEL, STATE_LABEL, formatDuration } from "../format";
import {
  ACTIVE_STATES,
  STAGES,
  costToday,
  errorHint,
  stageIndex,
  summarise,
  useLiveTasks,
  type DomainHealth,
  type LiveTask,
} from "../live";

const KIND_LABEL: Record<string, string> = { reel: "Reel" };

const STAGE_LABEL: Record<(typeof STAGES)[number], string> = {
  planned: "Geplant",
  running: "Worker",
  gate_check: "Prüfung",
  supervisor_qa: "Supervisor",
  done: "Fertig",
};

// Constellation layout: the orchestrator in the middle, one node per worker domain around it.
const HUB = { x: 200, y: 150 };
const NODES: Record<"trading" | "content" | "private", { x: number; y: number }> = {
  trading: { x: 70, y: 62 },
  content: { x: 330, y: 62 },
  private: { x: 200, y: 262 },
};
const NODE_R = 44;
const RING = 2 * Math.PI * NODE_R;

function ringShare(tasks: readonly LiveTask[], domain: string): number {
  const active = tasks.filter((t) => t.domain === domain && ACTIVE_STATES.has(t.state));
  if (active.length === 0) return 0;
  const sum = active.reduce((acc, t) => acc + stageIndex(t.state), 0);
  return sum / (active.length * (STAGES.length - 1));
}

function nodeStatus(h: DomainHealth): string {
  if (h.escalated > 0) return `⚠ ${String(h.escalated)} eskaliert`;
  if (h.waiting > 0) return `${String(h.waiting)} wartet`;
  if (h.active > 0) return `${String(h.active)} aktiv`;
  return "ruhig";
}

function Constellation({ tasks, health }: { tasks: LiveTask[]; health: DomainHealth[] }) {
  const core = health.find((h) => h.domain === "core");
  const coreActive = (core?.active ?? 0) > 0;
  return (
    <svg className="constellation" viewBox="0 0 400 320" role="img" aria-labelledby="const-title">
      <title id="const-title">
        {health.map((h) => `${DOMAIN_LABEL[h.domain] ?? h.domain}: ${nodeStatus(h)}`).join(", ")}
      </title>
      {(Object.keys(NODES) as (keyof typeof NODES)[]).map((domain) => {
        const h = health.find((x) => x.domain === domain);
        const busy = (h?.active ?? 0) > 0;
        const p = NODES[domain];
        return (
          <line
            key={`l-${domain}`}
            x1={HUB.x}
            y1={HUB.y}
            x2={p.x}
            y2={p.y}
            className={`link link-${domain}${busy ? " link-busy" : ""}`}
          />
        );
      })}
      <g className={`hub${coreActive ? " hub-busy" : ""}`}>
        <circle cx={HUB.x} cy={HUB.y} r={60} className="hub-glow" />
        <circle cx={HUB.x} cy={HUB.y} r={52} className="hub-core" />
        <text x={HUB.x} y={HUB.y - 4} className="node-title">
          Orchestrator
        </text>
        <text x={HUB.x} y={HUB.y + 14} className="node-sub">
          {core ? nodeStatus(core) : "ruhig"}
        </text>
      </g>
      {(Object.keys(NODES) as (keyof typeof NODES)[]).map((domain) => {
        const h = health.find((x) => x.domain === domain);
        if (!h) return null;
        const p = NODES[domain];
        const share = ringShare(tasks, domain);
        return (
          <g key={domain} className={`node node-${domain}${h.escalated > 0 ? " node-alert" : ""}`}>
            <circle cx={p.x} cy={p.y} r={NODE_R} className="node-base" />
            <circle
              cx={p.x}
              cy={p.y}
              r={NODE_R}
              className="node-ring"
              strokeDasharray={`${String(RING * share)} ${String(RING)}`}
              transform={`rotate(-90 ${String(p.x)} ${String(p.y)})`}
            />
            <text x={p.x} y={p.y - 3} className="node-title">
              {DOMAIN_LABEL[domain]}
            </text>
            <text x={p.x} y={p.y + 14} className="node-sub">
              {nodeStatus(h)}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

function RetryReels() {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const retry = (): void => {
    setBusy(true);
    setMessage("");
    retryFailedReels()
      .then((r) => {
        setMessage(
          r.retried === 0
            ? "Keine fehlgeschlagenen Reels zum Wiederholen."
            : `${String(r.retried)} Reels sind wieder in der Warteschlange` +
                (r.still_failed > 0 ? `, ${String(r.still_failed)} folgen, sobald Platz ist.` : "."),
        );
      })
      .catch((err: unknown) => {
        setMessage(err instanceof ApiError ? err.message : "Das hat nicht geklappt.");
      })
      .finally(() => {
        setBusy(false);
      });
  };
  return (
    <div className="retry">
      <button type="button" disabled={busy} onClick={retry}>
        Fehlgeschlagene Reels erneut versuchen
      </button>
      <p className="muted message" role="status">
        {message}
      </p>
    </div>
  );
}

function Stepper({ state }: { state: LiveTask["state"] }) {
  const at = stageIndex(state);
  const failed = state === "escalated";
  return (
    <ol className="stepper" aria-label={`Stand: ${STATE_LABEL[state] ?? state}`}>
      {STAGES.map((stage, i) => (
        <li
          key={stage}
          className={
            i < at ? "step-done" : i === at ? (failed ? "step-failed" : "step-now") : "step-todo"
          }
        >
          {failed && i === at
            ? "Eskaliert"
            : state === "awaiting_action" && i === at
              ? "Wartet auf dich"
              : STAGE_LABEL[stage]}
        </li>
      ))}
    </ol>
  );
}

function Lane({ task }: { task: LiveTask }) {
  return (
    <li className={`lane live-lane state-${task.state}`}>
      <div className="lane-head">
        <span className={`domain domain-${task.domain}`}>{DOMAIN_LABEL[task.domain]}</span>
        <span>{task.kind ? (KIND_LABEL[task.kind] ?? task.kind) : "Aufgabe"}</span>
        <span className="mono muted small-id">{task.task_id.slice(0, 8)}</span>
        {task.node && <span className="mono muted">{task.node}</span>}
        <span className="muted lane-meta">
          {formatDuration(task.duration_ms)}
          {task.cost_eur > 0 && ` · ${task.cost_eur.toFixed(4)} €`}
        </span>
      </div>
      <Stepper state={task.state} />
      {task.error_class && (
        <p className="critical lane-error">
          <span className="mono">{task.error_class}</span> · {errorHint(task.error_class)}
        </p>
      )}
    </li>
  );
}

export function Agents() {
  const { tasks, link, snapshotError } = useLiveTasks();
  const health = summarise(tasks);
  const total = (key: keyof Omit<DomainHealth, "domain">): number =>
    health.reduce((acc, h) => acc + h[key], 0);
  const escalated = tasks.filter((t) => t.state === "escalated");
  const linkLabel =
    link === "live" ? "● Live verbunden" : link === "connecting" ? "○ Verbinde …" : "○ Getrennt";

  return (
    <div className="agents">
      <h1>Agenten</h1>
      <p className="question">Ist der Schwarm gesund, und was kostet er?</p>

      <div className="kpis" role="list">
        <div className="kpi" role="listitem">
          <span className="kpi-value">{total("active")}</span>
          <span className="muted">arbeiten gerade</span>
        </div>
        <div className="kpi" role="listitem">
          <span className="kpi-value warn">{total("waiting")}</span>
          <span className="muted">warten auf Freigabe</span>
        </div>
        <div className="kpi" role="listitem">
          <span className={`kpi-value${total("escalated") > 0 ? " critical" : ""}`}>
            {total("escalated")}
          </span>
          <span className="muted">eskaliert</span>
        </div>
        <div className="kpi" role="listitem">
          <span className="kpi-value positive">{total("done")}</span>
          <span className="muted">fertig</span>
        </div>
        <div className="kpi" role="listitem">
          <span className="kpi-value mono">{costToday(tasks, new Date()).toFixed(2)} €</span>
          <span className="muted">Modellkosten heute</span>
        </div>
        <div className="kpi" role="listitem">
          <span className={`kpi-link link-state-${link}`}>{linkLabel}</span>
          <span className="muted">Telemetrie</span>
        </div>
      </div>

      <div className="live-grid">
        <section className="card" aria-labelledby="swarm-title">
          <h2 id="swarm-title">Schwarm</h2>
          <Constellation tasks={tasks} health={health} />
          <p className="muted legend">
            Ring = Fortschritt im Loop (Worker → Prüfung → Supervisor). Leuchtende Linien:
            der Orchestrator hat dort gerade Arbeit verteilt. Privat zeigt nur Metadaten.
          </p>
        </section>

        <section className="card" aria-labelledby="esc-title">
          <h2 id="esc-title">Eskalationen</h2>
          {escalated.length === 0 && <p className="muted">Nichts eskaliert. Alles im Rahmen.</p>}
          <ul className="lanes">
            {escalated.slice(0, 8).map((t) => (
              <li key={t.task_id} className="lane state-escalated esc-item">
                <span className={`domain domain-${t.domain}`}>{DOMAIN_LABEL[t.domain]}</span>
                <span className="mono">{t.error_class ?? "unbekannt"}</span>
                <span className="muted">{errorHint(t.error_class)}</span>
              </li>
            ))}
          </ul>
          {escalated.some((t) => t.kind === "reel") && <RetryReels />}
        </section>
      </div>

      <section className="card" aria-labelledby="lanes-title">
        <h2 id="lanes-title">Aufgaben</h2>
        {snapshotError && <p className="critical">{snapshotError}</p>}
        {tasks.length === 0 && !snapshotError && (
          <p className="muted">
            Noch keine Aufgaben. Reiche unter Content ein Reel ein, dann erscheint es hier live.
          </p>
        )}
        <ul className="lanes">
          {tasks.map((t) => (
            <Lane key={t.task_id} task={t} />
          ))}
        </ul>
      </section>
    </div>
  );
}
