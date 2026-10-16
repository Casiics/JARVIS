import type { DashboardInfo } from "../dashboards";

export function Placeholder({ dashboard }: { dashboard: DashboardInfo }) {
  return (
    <div>
      <h1>{dashboard.title}</h1>
      <p className="question">{dashboard.question}</p>
      <section className="card">
        <p className="muted">
          Die Daten für diese Ansicht kommen mit Phase {String(dashboard.dataPhase ?? "–")} der
          Roadmap. Das Gerüst, die Navigation und Deep Links stehen schon.
        </p>
      </section>
    </div>
  );
}
