// Live, metadata-only telemetry. Messages that fail the schema are dropped, never rendered.
import { useEffect, useState } from "react";
import { TelemetryEvent } from "./schemas";

const MAX_TASKS = 50;

export function parseTelemetry(raw: unknown): TelemetryEvent | null {
  if (typeof raw !== "string") return null;
  try {
    const parsed = TelemetryEvent.safeParse(JSON.parse(raw));
    return parsed.success ? parsed.data : null;
  } catch {
    return null;
  }
}

/** Latest event per task, newest first. */
export function mergeEvent(tasks: TelemetryEvent[], event: TelemetryEvent): TelemetryEvent[] {
  return [event, ...tasks.filter((t) => t.task_id !== event.task_id)].slice(0, MAX_TASKS);
}

export type LinkState = "connecting" | "live" | "offline";

export function useTelemetry(): { tasks: TelemetryEvent[]; link: LinkState } {
  const [tasks, setTasks] = useState<TelemetryEvent[]>([]);
  const [link, setLink] = useState<LinkState>("connecting");

  useEffect(() => {
    let socket: WebSocket | null = null;
    let retry: number | undefined;
    let closed = false;

    const connect = (): void => {
      const scheme = window.location.protocol === "https:" ? "wss" : "ws";
      socket = new WebSocket(`${scheme}://${window.location.host}/ws/telemetry`);
      socket.onopen = () => {
        setLink("live");
      };
      socket.onmessage = (message: MessageEvent<unknown>) => {
        const event = parseTelemetry(message.data);
        if (event) setTasks((current) => mergeEvent(current, event));
      };
      socket.onclose = () => {
        setLink("offline");
        if (!closed) retry = window.setTimeout(connect, 5000);
      };
    };
    connect();
    return () => {
      closed = true;
      window.clearTimeout(retry);
      socket?.close();
    };
  }, []);

  return { tasks, link };
}
