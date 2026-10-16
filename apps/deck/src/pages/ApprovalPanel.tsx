import { useEffect, useState } from "react";
import { decide, getApproval } from "../api";
import { DOMAIN_LABEL, formatAge } from "../format";
import type { ApprovalDetail } from "../schemas";

type Phase = "loading" | "ready" | "deciding" | "done" | "error";

export function ApprovalPanel({ approvalId, onClose }: { approvalId: string; onClose: () => void }) {
  const [detail, setDetail] = useState<ApprovalDetail | null>(null);
  const [phase, setPhase] = useState<Phase>("loading");
  const [message, setMessage] = useState<string>("");

  useEffect(() => {
    getApproval(approvalId)
      .then((d) => {
        setDetail(d);
        setPhase(d.status === "pending" ? "ready" : "done");
        if (d.status !== "pending") setMessage(`Bereits entschieden: ${d.status}`);
      })
      .catch(() => {
        setPhase("error");
        setMessage("Diese Freigabe konnte nicht geladen werden.");
      });
  }, [approvalId]);

  const run = (decision: "approve" | "reject"): void => {
    setPhase("deciding");
    setMessage("Bestätige mit deinem Passkey …");
    decide(approvalId, decision)
      .then((result) => {
        setPhase("done");
        setMessage(result.status === "approved" ? "Freigegeben." : "Abgelehnt.");
      })
      .catch(() => {
        setPhase("ready");
        setMessage("Der Passkey wurde nicht bestätigt. Nichts wurde entschieden.");
      });
  };

  return (
    <aside className="panel" role="dialog" aria-modal="true" aria-labelledby="panel-title">
      <header className="panel-head">
        <h2 id="panel-title">Freigabe prüfen</h2>
        <button type="button" className="ghost" onClick={onClose}>
          Schließen
        </button>
      </header>
      {detail && (
        <dl className="facts">
          <dt>Domäne</dt>
          <dd>{DOMAIN_LABEL[detail.domain]}</dd>
          <dt>Aktion</dt>
          <dd className="mono">{detail.tool}</dd>
          <dt>Stufe</dt>
          <dd>
            {detail.tier} ({detail.gate_outcome === "needs_step_up" ? "Step-up" : "Freigabe"})
          </dd>
          <dt>Grund der Sperre</dt>
          <dd>{detail.gate_reason}</dd>
          {detail.tainted && (
            <>
              <dt>Herkunft</dt>
              <dd className="warn">Die Aufgabe hat externe Inhalte gesehen. Prüfe die Parameter genau.</dd>
            </>
          )}
          <dt>Begründung</dt>
          <dd>{detail.rationale ?? "Privat: nur Metadaten sichtbar."}</dd>
          {detail.parameters && (
            <>
              <dt>Parameter</dt>
              <dd>
                <pre className="mono">{JSON.stringify(detail.parameters, null, 2)}</pre>
              </dd>
            </>
          )}
          <dt>Wartet seit</dt>
          <dd>{formatAge(detail.age_s)}</dd>
          <dt>Prüfsumme</dt>
          <dd className="mono small">{detail.request_hash}</dd>
        </dl>
      )}
      <p className="message" role="status">
        {message}
      </p>
      {phase === "ready" && (
        <div className="decide">
          <button type="button" className="primary" onClick={() => { run("approve"); }}>
            Mit Passkey freigeben
          </button>
          <button type="button" className="danger" onClick={() => { run("reject"); }}>
            Mit Passkey ablehnen
          </button>
        </div>
      )}
    </aside>
  );
}
