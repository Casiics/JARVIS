import { Link, useSearchParams } from "react-router-dom";
import { DOMAIN_LABEL, STATE_LABEL, formatAge } from "../format";
import { useTelemetry } from "../telemetry";
import { useAttentionContext } from "../useAttention";
import { ApprovalPanel } from "./ApprovalPanel";

export function Home() {
  const { attention, refresh, error } = useAttentionContext();
  const { tasks, link } = useTelemetry();
  const [params, setParams] = useSearchParams();
  const open = params.get("approval");

  const close = (): void => {
    setParams({});
    refresh();
  };

  return (
    <div className="home">
      <h1>Zentrale</h1>
      <p className="question">Was braucht mich heute?</p>

      <section className="card" aria-labelledby="attention-title">
        <h2 id="attention-title">Wartet auf dich</h2>
        {error && <p className="critical">{error}</p>}
        {attention && attention.count === 0 && <p className="muted">Keine offenen Freigaben.</p>}
        <ul className="queue">
          {attention?.approvals.map((a) => (
            <li key={a.approval_id}>
              <button
                type="button"
                className="queue-item"
                onClick={() => {
                  setParams({ approval: a.approval_id });
                }}
              >
                <span className={`domain domain-${a.domain}`}>{DOMAIN_LABEL[a.domain]}</span>
                <span className="mono">{a.tool}</span>
                <span className="tier">Stufe {a.tier}</span>
                {a.tainted && <span className="warn">⚠ externe Inhalte</span>}
                <span className="muted">{formatAge(a.age_s)}</span>
                <span className="action">Prüfen →</span>
              </button>
            </li>
          ))}
        </ul>
      </section>

      <section className="card" aria-labelledby="agents-title">
        <h2 id="agents-title">
          Live-Agenten <span className="muted">({link === "live" ? "verbunden" : link === "connecting" ? "verbinde" : "getrennt"})</span>
        </h2>
        {tasks.length === 0 && <p className="muted">Gerade arbeitet kein Agent.</p>}
        <p>
          <Link to="/agents" className="more">
            Schwarm, Aufgaben und Eskalationen ansehen →
          </Link>
        </p>
        <ul className="lanes">
          {tasks.map((t) => (
            <li key={t.task_id} className={`lane state-${t.state}`}>
              <span className={`domain domain-${t.domain}`}>{DOMAIN_LABEL[t.domain]}</span>
              <span className="mono">{t.node}</span>
              <span>{STATE_LABEL[t.state]}</span>
              {t.error_class && <span className="critical">{t.error_class}</span>}
              <span className="muted mono">{t.cost_eur.toFixed(4)} €</span>
            </li>
          ))}
        </ul>
      </section>

      {open && <ApprovalPanel approvalId={open} onClose={close} />}
    </div>
  );
}
