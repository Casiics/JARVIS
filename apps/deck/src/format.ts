export function formatAge(seconds: number): string {
  if (seconds < 60) return "gerade eben";
  if (seconds < 3600) return `vor ${String(Math.floor(seconds / 60))} min`;
  if (seconds < 86400) return `vor ${String(Math.floor(seconds / 3600))} h`;
  return `vor ${String(Math.floor(seconds / 86400))} Tagen`;
}

export const DOMAIN_LABEL: Record<string, string> = {
  core: "Core",
  trading: "Trading",
  content: "Content",
  private: "Privat",
};

export const STATE_LABEL: Record<string, string> = {
  planned: "geplant",
  running: "arbeitet",
  gate_check: "Prüfung",
  supervisor_qa: "Supervisor",
  awaiting_action: "wartet auf Freigabe",
  executing: "führt aus",
  done: "fertig",
  escalated: "eskaliert",
};

export const REEL_STATUS_LABEL: Record<string, string> = {
  queued: "wartet auf den Worker",
  running: "wird verarbeitet",
  done: "fertig",
  awaiting_action: "braucht dich",
  failed: "fehlgeschlagen",
};

export function formatDuration(ms: number | null): string {
  if (ms === null) return "";
  const seconds = Math.round(ms / 1000);
  return seconds < 60 ? `${String(seconds)} s` : `${String(Math.floor(seconds / 60))} min ${String(seconds % 60)} s`;
}
