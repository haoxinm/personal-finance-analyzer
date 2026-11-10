import * as React from "react";

export interface JobEvent {
  status: "queued" | "running" | "done" | "error";
  progress: number; // 0..1
  message?: string;
  error_detail?: string;
  /** Finished re-parse job: hand edits kept on the rebuilt rows / dropped for lack of a match. */
  restored_edits?: number | null;
  unmatched_edits?: number | null;
}

export interface UseJobProgressResult {
  event: JobEvent | null;
  connectionState: "idle" | "connecting" | "open" | "closed" | "error";
}

const RECONNECT_BASE_MS = 1000;
const RECONNECT_MAX_MS = 15000;

function readPfaToken(): string | undefined {
  if (typeof document === "undefined") return undefined;
  return document.querySelector('meta[name="pfa-token"]')?.getAttribute("content") ?? undefined;
}

/**
 * Subscribes to `GET /api/jobs/{id}/events` (§3.12), an SSE progress stream, with automatic
 * reconnect (capped exponential backoff) until the job reaches a terminal state (`done` or
 * `error`) or the caller unmounts / passes a different `jobId`.
 *
 * The per-launch token (A31) travels as a query parameter, since `EventSource` cannot set
 * request headers.
 */
export function useJobProgress(jobId: string | null | undefined): UseJobProgressResult {
  const [event, setEvent] = React.useState<JobEvent | null>(null);
  const [connectionState, setConnectionState] =
    React.useState<UseJobProgressResult["connectionState"]>("idle");

  React.useEffect(() => {
    if (!jobId) {
      setEvent(null);
      setConnectionState("idle");
      return;
    }

    let cancelled = false;
    let source: EventSource | null = null;
    let reconnectAttempt = 0;
    let reconnectTimer: ReturnType<typeof setTimeout> | undefined;

    const connect = () => {
      if (cancelled) return;
      setConnectionState("connecting");

      const token = readPfaToken();
      const url = new URL(`/api/jobs/${encodeURIComponent(jobId)}/events`, window.location.origin);
      if (token) url.searchParams.set("token", token);

      source = new EventSource(url.toString());

      source.onopen = () => {
        if (cancelled) return;
        reconnectAttempt = 0;
        setConnectionState("open");
      };

      source.onmessage = (message: MessageEvent<string>) => {
        if (cancelled) return;
        try {
          const parsed = JSON.parse(message.data) as JobEvent;
          setEvent(parsed);
          if (parsed.status === "done" || parsed.status === "error") {
            source?.close();
            setConnectionState("closed");
          }
        } catch {
          // Malformed frame: ignore it rather than tearing down the stream.
        }
      };

      source.onerror = () => {
        if (cancelled) return;
        source?.close();
        setConnectionState("error");
        const delay = Math.min(RECONNECT_BASE_MS * 2 ** reconnectAttempt, RECONNECT_MAX_MS);
        reconnectAttempt += 1;
        reconnectTimer = setTimeout(connect, delay);
      };
    };

    connect();

    return () => {
      cancelled = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      source?.close();
    };
  }, [jobId]);

  return { event, connectionState };
}
