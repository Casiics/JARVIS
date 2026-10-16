import { useCallback, useEffect, useState } from "react";
import { useOutletContext } from "react-router-dom";
import { getAttention } from "./api";
import type { Attention } from "./schemas";

const POLL_MS = 15000;

export interface AttentionState {
  attention: Attention | null;
  refresh: () => void;
  error: string | null;
}

/** Shared by the shell (badge) and the dashboards through the router outlet context. */
export function useAttentionContext(): AttentionState {
  return useOutletContext<AttentionState>();
}

export function useAttention(): AttentionState {
  const [attention, setAttention] = useState<Attention | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(() => {
    getAttention()
      .then((a) => {
        setAttention(a);
        setError(null);
      })
      .catch(() => {
        setError("Die Aufmerksamkeitsliste konnte nicht geladen werden.");
      });
  }, []);

  useEffect(() => {
    refresh();
    const timer = window.setInterval(refresh, POLL_MS);
    return () => {
      window.clearInterval(timer);
    };
  }, [refresh]);

  return { attention, refresh, error };
}
