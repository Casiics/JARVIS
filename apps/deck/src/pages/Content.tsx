import { useCallback, useEffect, useState, type SyntheticEvent } from "react";
import { ApiError, getReels, submitReel } from "../api";
import { REEL_STATUS_LABEL, formatAge, formatDuration } from "../format";
import type { Reel } from "../schemas";
import { ContextReviews } from "./ContextReviews";

const POLL_MS = 5000;

function ageSeconds(iso: string): number {
  return Math.max(0, Math.floor((Date.now() - Date.parse(iso)) / 1000));
}

export function Content() {
  const [reels, setReels] = useState<Reel[] | null>(null);
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [loadError, setLoadError] = useState("");

  const load = useCallback(() => {
    getReels()
      .then((data) => {
        setReels(data.reels);
        setLoadError("");
      })
      .catch((err: unknown) => {
        setLoadError(
          err instanceof ApiError && err.status === 503
            ? "Der Reel-Eingang ist auf diesem Server nicht eingerichtet."
            : "Die Liste konnte nicht geladen werden.",
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

  const submit = (event: SyntheticEvent<HTMLFormElement>): void => {
    event.preventDefault();
    setBusy(true);
    setMessage("");
    submitReel(url.trim())
      .then((result) => {
        setUrl("");
        setMessage(
          result.created
            ? `Reel ${result.reel.reel_code} ist in der Warteschlange.`
            : `Reel ${result.reel.reel_code} ist schon in der Liste.`,
        );
        load();
      })
      .catch((err: unknown) => {
        setMessage(err instanceof ApiError ? err.message : "Das hat nicht geklappt.");
      })
      .finally(() => {
        setBusy(false);
      });
  };

  const waiting = reels?.filter((r) => r.status === "queued").length ?? 0;

  return (
    <div>
      <h1>Content</h1>
      <p className="question">Was ist in der Pipeline, und was ist bereit zur Veröffentlichung?</p>

      <section className="card" aria-labelledby="intake-title">
        <h2 id="intake-title">Reel einwerfen</h2>
        <form className="intake" onSubmit={submit}>
          <label htmlFor="reel-url" className="muted">
            Instagram-Link (Teilen → Link kopieren)
          </label>
          <div className="intake-row">
            <input
              id="reel-url"
              type="url"
              inputMode="url"
              placeholder="https://www.instagram.com/reel/…"
              autoComplete="off"
              value={url}
              onChange={(e) => {
                setUrl(e.target.value);
              }}
            />
            <button type="submit" className="primary" disabled={busy || url.trim() === ""}>
              Verarbeiten
            </button>
          </div>
        </form>
        <p role="status" className="message">
          {message}
        </p>
      </section>

      <section className="card" aria-labelledby="reels-title">
        <h2 id="reels-title">
          Reels {waiting > 0 && <span className="muted">({String(waiting)} warten)</span>}
        </h2>
        {loadError && <p className="critical">{loadError}</p>}
        {reels?.length === 0 && <p className="muted">Noch keine Reels eingeworfen.</p>}
        <ul className="lanes">
          {reels?.map((r) => (
            <li key={r.task_id} className={`lane reel-${r.status}`}>
              <a className="mono" href={r.source_ref} target="_blank" rel="noreferrer noopener">
                {r.reel_code}
              </a>
              <span>{REEL_STATUS_LABEL[r.status]}</span>
              {r.error_class && <span className="critical mono">{r.error_class}</span>}
              <span className="muted">{formatDuration(r.duration_ms)}</span>
              <span className="muted reel-age">{formatAge(ageSeconds(r.submitted_at))}</span>
            </li>
          ))}
        </ul>
      </section>

      <ContextReviews />
    </div>
  );
}
