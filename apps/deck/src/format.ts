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

// Why a reel failed, as the content worker reports it (mod_content/reel/pipeline.py). The
// worker only ever sends these fixed classes; unknown ones are shown as they are.
export const REEL_ERROR_TEXT: Record<string, string> = {
  fetch_login_required:
    "Instagram verlangt eine Anmeldung oder bremst gerade ab. Später erneut versuchen; ohne Konto lädt JARVIS nur öffentliche Reels.",
  fetch_not_available: "Das Reel ist nicht (mehr) öffentlich verfügbar oder der Link stimmt nicht.",
  fetch_proxy_unreachable:
    "Der Download-Proxy ist nicht erreichbar. Läuft egress-reel? (docker compose -f docker/docker-compose.yml up -d egress-reel)",
  fetch_network_error: "Keine Verbindung zu Instagram (DNS oder Zeitüberschreitung).",
  fetch_too_large: "Das Video ist zu groß oder zu lang.",
  fetch_no_video: "Der Download lieferte kein Video.",
  fetch_failed:
    "Download fehlgeschlagen, Ursache unbekannt. Den Worker mit --show-sandbox-errors starten, dann steht der Grund im Terminal.",
  fetch_timeout: "Der Download hat zu lange gedauert.",
  fetch_sandbox_start_failed:
    "Die Download-Sandbox startet nicht. Meist fehlt das Image jarvis-reel-media (siehe Runbook).",
  extract_failed: "Bilder und Ton konnten nicht aus dem Video gelesen werden.",
  extract_timeout: "Das Auslesen des Videos hat zu lange gedauert.",
  extract_sandbox_start_failed: "Die Sandbox zum Auslesen startet nicht (Image jarvis-reel-media).",
  transcribe_failed: "Die Transkription ist fehlgeschlagen. Liegen die Whisper-Modelle im Modellordner?",
  transcribe_timeout: "Die Transkription hat zu lange gedauert.",
  transcribe_sandbox_start_failed:
    "Die Whisper-Sandbox startet nicht. Fehlt das Image jarvis-reel-whisper oder der GPU-Zugriff?",
  media_rejected: "Die Sandbox lieferte Dateien, die die Prüfung nicht bestanden haben.",
  model_unavailable: "Das lokale Modell antwortet nicht. Läuft Ollama?",
  routing_denied: "Für diese Daten gibt es keinen erlaubten Modellweg.",
  tool_not_allowed: "Der Auftrag erlaubt diesen Schritt nicht.",
  max_iterations: "Nach vier Versuchen kein gültiges Ergebnis. Ein Mensch muss draufschauen.",
  internal_error: "Interner Fehler im Worker. Das Terminal des Workers zeigt mehr.",
};

export function reelErrorText(errorClass: string): string {
  return REEL_ERROR_TEXT[errorClass] ?? errorClass;
}

export function formatDuration(ms: number | null): string {
  if (ms === null) return "";
  const seconds = Math.round(ms / 1000);
  return seconds < 60 ? `${String(seconds)} s` : `${String(Math.floor(seconds / 60))} min ${String(seconds % 60)} s`;
}
