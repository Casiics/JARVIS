import { useEffect, useId, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ApiError, logout, retryFailedReels } from "./api";
import { DASHBOARDS } from "./dashboards";
import { filterCommands, isPaletteShortcut, moveSelection, type Command } from "./palette";

const KEYWORDS: Record<string, readonly string[]> = {
  "/": ["home", "start", "freigaben", "heute"],
  "/agents": ["schwarm", "orchestrator", "eskalation", "hermes", "kosten", "live"],
  "/trading": ["bots", "risiko", "depot", "kill-switch"],
  "/evolution": ["skills", "lernen", "verbesserung"],
  "/content": ["reels", "pipeline", "kontext", "instagram"],
  "/intel": ["gespeichert", "social", "insta"],
  "/finance": ["kosten", "budget", "geld"],
};

const ACTIONS: readonly Command[] = [
  {
    id: "action:reel",
    title: "Reel einreichen",
    hint: "Öffnet Content mit dem Eingabefeld",
    group: "Aktion",
    keywords: ["link", "instagram", "neu", "submit"],
  },
  {
    id: "action:retry",
    title: "Fehlgeschlagene Reels erneut versuchen",
    hint: "Stellt sie wieder in die Warteschlange",
    group: "Aktion",
    keywords: ["retry", "wiederholen", "fetch failed", "fehler"],
  },
  {
    id: "action:logout",
    title: "Abmelden",
    hint: "Beendet diese Sitzung",
    group: "Aktion",
    keywords: ["logout", "sign out"],
  },
];

const COMMANDS: readonly Command[] = [
  ...DASHBOARDS.map(
    (d): Command => ({
      id: `nav:${d.path}`,
      title: d.title,
      hint: d.question,
      group: "Navigation",
      keywords: KEYWORDS[d.path] ?? [],
    }),
  ),
  ...ACTIONS,
];

export function CommandPalette({ onSignedOut }: { onSignedOut: () => void }) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState(0);
  const [notice, setNotice] = useState("");
  const input = useRef<HTMLInputElement>(null);
  const opener = useRef<HTMLButtonElement>(null);
  const navigate = useNavigate();
  const listId = useId();
  const results = useMemo(() => filterCommands(COMMANDS, query), [query]);

  const show = (): void => {
    setQuery("");
    setSelected(0);
    setNotice("");
    setOpen(true);
  };
  const hide = (): void => {
    setOpen(false);
    opener.current?.focus();
  };

  useEffect(() => {
    const onKey = (e: KeyboardEvent): void => {
      if (!isPaletteShortcut(e)) return;
      e.preventDefault();
      if (open) hide();
      else show();
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
    };
  });

  useEffect(() => {
    if (open) input.current?.focus();
  }, [open]);

  const run = (command: Command): void => {
    if (command.id.startsWith("nav:")) {
      hide();
      void navigate(command.id.slice(4));
      return;
    }
    if (command.id === "action:reel") {
      hide();
      void navigate("/content");
      return;
    }
    if (command.id === "action:logout") {
      hide();
      void logout().finally(onSignedOut);
      return;
    }
    if (command.id === "action:retry") {
      setNotice("Wird erneut versucht …");
      retryFailedReels()
        .then((r) => {
          setNotice(
            r.retried === 0
              ? "Keine fehlgeschlagenen Reels zum Wiederholen."
              : `${String(r.retried)} Reels sind wieder in der Warteschlange.`,
          );
        })
        .catch((err: unknown) => {
          setNotice(err instanceof ApiError ? err.message : "Das hat nicht geklappt.");
        });
    }
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>): void => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      setSelected((s) => moveSelection(s, e.key === "ArrowDown" ? 1 : -1, results.length));
    } else if (e.key === "Enter") {
      e.preventDefault();
      const command = results[selected];
      if (command) run(command);
    } else if (e.key === "Escape") {
      e.preventDefault();
      hide();
    }
  };

  const active = results[selected];
  return (
    <>
      <button type="button" ref={opener} className="palette-open" onClick={show}>
        <span>Suchen oder Befehl …</span>
        <kbd>⌘K</kbd>
      </button>
      {open && (
        <div
          className="palette-backdrop"
          onMouseDown={(e) => {
            if (e.target === e.currentTarget) hide();
          }}
        >
          <div className="palette" role="dialog" aria-modal="true" aria-label="Befehle">
            <input
              ref={input}
              className="palette-input"
              role="combobox"
              aria-expanded="true"
              aria-controls={listId}
              aria-activedescendant={active ? `${listId}-${active.id}` : undefined}
              placeholder="Dashboard oder Aktion suchen"
              value={query}
              onChange={(e) => {
                setQuery(e.target.value);
                setSelected(0);
                setNotice("");
              }}
              onKeyDown={onKeyDown}
            />
            <ul id={listId} role="listbox" className="palette-list">
              {results.map((c, i) => (
                <li
                  key={c.id}
                  id={`${listId}-${c.id}`}
                  role="option"
                  aria-selected={i === selected}
                  className={i === selected ? "palette-item selected" : "palette-item"}
                  onMouseEnter={() => {
                    setSelected(i);
                  }}
                  onClick={() => {
                    run(c);
                  }}
                >
                  <span className="palette-title">{c.title}</span>
                  <span className="muted palette-hint">
                    {c.group === "Aktion" ? "Aktion · " : ""}
                    {c.hint}
                  </span>
                </li>
              ))}
              {results.length === 0 && <li className="muted palette-empty">Nichts gefunden.</li>}
            </ul>
            <p className="muted palette-foot" role="status">
              {notice || "↑↓ wählen · Enter ausführen · Esc schließen"}
            </p>
          </div>
        </div>
      )}
    </>
  );
}
