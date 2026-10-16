import { NavLink, Outlet } from "react-router-dom";
import { logout } from "./api";
import { DASHBOARDS } from "./dashboards";
import { useAttention } from "./useAttention";

export function Shell({ onSignedOut }: { onSignedOut: () => void }) {
  const attentionState = useAttention();
  const { attention } = attentionState;
  const approvals = attention?.count ?? 0;
  const reviews = attention?.context_reviews ?? 0;
  const waiting = approvals + reviews;

  return (
    <div className="shell">
      <nav className="nav" aria-label="Dashboards">
        <div className="brand">JARVIS</div>
        {DASHBOARDS.map((d) => (
          <NavLink key={d.path} to={d.path} end={d.path === "/"} className="nav-item">
            <span>{d.title}</span>
            {d.path === "/" && approvals > 0 && (
              <span className="badge" aria-label={`${String(approvals)} Freigaben offen`}>
                {approvals}
              </span>
            )}
            {d.path === "/content" && reviews > 0 && (
              <span className="badge" aria-label={`${String(reviews)} Kontext-Freigaben offen`}>
                {reviews}
              </span>
            )}
          </NavLink>
        ))}
      </nav>
      <div className="main">
        <header className="topbar">
          <span className={`status ${waiting > 0 ? "status-warn" : "status-ok"}`}>
            {waiting > 0 ? `● ${String(waiting)} wartet auf dich` : "● Alles ruhig"}
          </span>
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
