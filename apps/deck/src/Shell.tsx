import { Link, NavLink, Outlet } from "react-router-dom";
import { logout } from "./api";
import { DASHBOARDS } from "./dashboards";
import { useEscalationCount } from "./live";
import { useAttention } from "./useAttention";

export function Shell({ onSignedOut }: { onSignedOut: () => void }) {
  const attentionState = useAttention();
  const { attention } = attentionState;
  const waiting = attention?.count ?? 0;
  const escalated = useEscalationCount();

  return (
    <div className="shell">
      <nav className="nav" aria-label="Dashboards">
        <div className="brand">JARVIS</div>
        {DASHBOARDS.map((d) => (
          <NavLink key={d.path} to={d.path} end={d.path === "/"} className="nav-item">
            <span>{d.title}</span>
            {d.path === "/" && waiting > 0 && (
              <span className="badge" aria-label={`${String(waiting)} Freigaben offen`}>
                {waiting}
              </span>
            )}
            {d.path === "/agents" && escalated > 0 && (
              <span className="badge badge-critical" aria-label={`${String(escalated)} eskaliert`}>
                {escalated}
              </span>
            )}
          </NavLink>
        ))}
      </nav>
      <div className="main">
        <header className="topbar">
          <span className={`status ${waiting > 0 ? "status-warn" : "status-ok"}`}>
            {waiting > 0
              ? `● ${String(waiting)} wartet auf dich`
              : escalated > 0
                ? "● Keine Freigaben offen"
                : "● Alles ruhig"}
          </span>
          {escalated > 0 && (
            <Link to="/agents" className="status status-critical">
              ⚠ {escalated} eskaliert (24 h)
            </Link>
          )}
          <span className="muted">Kill-Switch und Tageskosten folgen mit dem Trading-Dienst</span>
          <button
            type="button"
            className="ghost"
            onClick={() => {
              void logout().finally(onSignedOut);
            }}
          >
            Abmelden
          </button>
        </header>
        <main>
          <Outlet context={attentionState} />
        </main>
      </div>
    </div>
  );
}
