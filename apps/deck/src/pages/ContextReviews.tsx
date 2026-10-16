import { useCallback, useEffect, useState } from "react";
import { ApiError, decideContextReview, getContextReviews } from "../api";
import type { ContextReview } from "../schemas";

const POLL_MS = 10000;

const PROPOSAL_LABEL: Record<ContextReview["proposal"], string> = {
  store: "neu speichern",
  merge: "bestehenden Eintrag ergänzen",
  discard: "verwerfen",
};

function isHttpsLink(ref: string): boolean {
  try {
    return new URL(ref).protocol === "https:";
  } catch {
    return false;
  }
}

/** What the curation pipeline was unsure about. The owner decides; content applies it. */
export function ContextReviews() {
  const [open, setOpen] = useState<ContextReview[] | null>(null);
  const [recent, setRecent] = useState<ContextReview[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState("");
  const [loadError, setLoadError] = useState("");

  const load = useCallback(() => {
    getContextReviews()
      .then((data) => {
        setOpen(data.open);
        setRecent(data.recent);
        setLoadError("");
      })
      .catch((err: unknown) => {
        setLoadError(
          err instanceof ApiError && err.status === 503
            ? "Kontext-Freigaben sind auf diesem Server nicht eingerichtet."
            : "Die Freigaben konnten nicht geladen werden.",
        );
      });
  }, []);

  useEffect(() => {
    load();
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") load();
    }, POLL_MS);
    return () => {
      window.clearInterval(timer);
    };
  }, [load]);

  const decide = (review: ContextReview, accept: boolean): void => {
    setBusy(review.brief_id);
    setMessage("");
    decideContextReview(review.brief_id, accept)
      .then(() => {
        setMessage(
          accept
            ? `„${review.title}“ kommt in den Kontext (${review.category}).`
            : `„${review.title}“ wird verworfen.`,
        );
        load();
      })
      .catch((err: unknown) => {
        setMessage(
          err instanceof ApiError ? err.message : "Das hat nicht geklappt.",
        );
      })
      .finally(() => {
        setBusy(null);
      });
  };

  return (
    <section className="card" aria-labelledby="context-title">
      <h2 id="context-title">
        Kontext-Freigaben{" "}
        {open && open.length > 0 && (
          <span className="muted">({String(open.length)} offen)</span>
        )}
      </h2>
      {loadError && <p className="critical">{loadError}</p>}
      {open?.length === 0 && (
        <p className="muted">
          Nichts offen. Sichere Fälle speichert die Pipeline selbst.
        </p>
      )}
      <ul className="lanes">
        {open?.map((r) => (
          <li
            key={r.brief_id}
            className={`review ${r.flags.length > 0 ? "review-flagged" : ""}`}
          >
            <div className="review-head">
              <span className="domain domain-content">{r.category}</span>
              <strong>{r.title}</strong>
              <span className="muted">
                Vorschlag: {PROPOSAL_LABEL[r.proposal]} · Konfidenz{" "}
                {r.confidence.toFixed(2)}
              </span>
            </div>
            {r.flags.length > 0 && (
              <p className="critical">Auffällig: {r.flags.join(", ")}</p>
            )}
            <p>{r.summary}</p>
            {r.facts.length > 0 && (
              <ul className="review-facts">
                {r.facts.map((f) => (
                  <li key={f}>{f}</li>
                ))}
              </ul>
            )}
            <p className="muted small">{r.reasons.join(" · ")}</p>
            <p className="small">
              {r.source_refs.map((ref) =>
                isHttpsLink(ref) ? (
                  <a
                    key={ref}
                    className="mono"
                    href={ref}
                    target="_blank"
                    rel="noreferrer noopener"
                  >
                    {ref}{" "}
                  </a>
                ) : (
                  <span key={ref} className="mono">
                    {ref}{" "}
                  </span>
                ),
              )}
            </p>
            <div className="decide">
              <button
                type="button"
                className="primary"
                disabled={busy !== null}
                onClick={() => {
                  decide(r, true);
                }}
              >
                In den Kontext
              </button>
              <button
                type="button"
                className="danger"
                disabled={busy !== null}
                onClick={() => {
                  decide(r, false);
                }}
              >
                Verwerfen
              </button>
            </div>
          </li>
        ))}
      </ul>
      <p role="status" className="message">
        {message}
      </p>
      {recent.length > 0 && (
        <details>
          <summary className="muted">Zuletzt entschieden</summary>
          <ul className="lanes">
            {recent.map((r) => (
              <li key={r.brief_id} className="lane">
                <span>{r.accepted ? "✓" : "✗"}</span>
                <span>{r.title}</span>
                <span className="muted">{r.category}</span>
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}
