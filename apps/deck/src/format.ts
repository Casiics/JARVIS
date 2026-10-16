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
