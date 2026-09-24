"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, eventsUrl, MOCK } from "@/lib/api";
import { TERMINAL } from "@/lib/format";
import type { Run } from "@/lib/types";

export interface LiveLine {
  t: number;
  kind: string;
  text: string;
  tone?: "pass" | "fail";
}

export type LiveMode = "sse" | "polling" | "idle";

const EVENTS = ["status", "log", "round", "tool", "browser"] as const;

function describe(kind: string, d: Record<string, unknown>): LiveLine | null {
  const t = Date.now();
  switch (kind) {
    case "status":
      return { t, kind, text: `Status → ${String(d.status)}` };
    case "log":
      return { t, kind, text: String(d.message ?? "") };
    case "tool":
      return { t, kind, text: `Agent ${String(d.tool)} ${String(d.target ?? "")}` };
    case "round":
      return { t, kind, text: `Agent round ${String(d.round)}: ${d.passed ? "all checks pass" : "checks failing"}`, tone: d.passed ? "pass" : "fail" };
    case "browser":
      return { t, kind, text: `Browser test "${String(d.test)}": ${d.passed ? "pass" : "fail"}`, tone: d.passed ? "pass" : "fail" };
    default:
      return null;
  }
}

/**
 * Run state with live updates: SSE from /api/runs/{id}/events, refetching the full run on
 * each event; falls back to polling if SSE is unavailable. Stops once the run is terminal.
 */
export function useRunLive(id: string) {
  const [run, setRun] = useState<Run | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [lines, setLines] = useState<LiveLine[]>([]);
  const [mode, setMode] = useState<LiveMode>("idle");
  const statusRef = useRef<string | null>(null);
  const refetchTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const seq = useRef(0);

  const load = useCallback(async () => {
    if (!id) return;
    const mine = ++seq.current;
    try {
      const r = await api.getRun(id);
      if (mine !== seq.current) return; // a newer fetch is in flight; never apply stale state
      statusRef.current = r.status;
      setRun(r);
      setError(null);
    } catch (e) {
      setError(e);
    }
  }, [id]);

  const soon = useCallback(() => {
    if (refetchTimer.current) clearTimeout(refetchTimer.current);
    refetchTimer.current = setTimeout(() => void load(), 350);
  }, [load]);

  useEffect(() => {
    if (!id) {
      setError(new Error("No run id in the URL."));
      return;
    }
    let es: EventSource | null = null;
    let poll: ReturnType<typeof setInterval> | null = null;
    let closed = false;

    const startPolling = (ms: number) => {
      if (poll) clearInterval(poll);
      poll = setInterval(() => {
        if (statusRef.current && TERMINAL.has(statusRef.current)) {
          stop();
          return;
        }
        void load();
      }, ms);
    };

    const stop = () => {
      closed = true;
      es?.close();
      es = null;
      if (poll) clearInterval(poll);
      poll = null;
      setMode("idle");
    };

    const handle = (kind: string, data: Record<string, unknown>) => {
      const line = describe(kind, data);
      if (line) setLines((ls) => [...ls.slice(-199), line]);
      if (kind === "status" && typeof data.status === "string") {
        statusRef.current = data.status;
        setRun((r) => (r ? { ...r, status: data.status as Run["status"] } : r));
        if (data.status.startsWith("awaiting_")) {
          void load(); // gates need the full state (requirements, results) right away
          return;
        }
        if (TERMINAL.has(data.status)) {
          // Final refetch picks up the finished state; then stop listening.
          void load().then(stop);
          return;
        }
      }
      soon();
    };

    void load().then(() => {
      if (closed) return;
      if (statusRef.current && TERMINAL.has(statusRef.current)) return;
      const url = eventsUrl(id);
      if (!url || typeof EventSource === "undefined") {
        setMode("polling");
        startPolling(MOCK ? 1500 : 3000);
        return;
      }
      es = new EventSource(url);
      setMode("sse");
      // Safety net: slow poll alongside SSE in case an event is missed.
      startPolling(15000);
      EVENTS.forEach((name) =>
        es!.addEventListener(name, (e) => {
          try {
            handle(name, JSON.parse((e as MessageEvent).data || "{}"));
          } catch {
            handle(name, {});
          }
        }),
      );
      es.onmessage = (e) => {
        try {
          const j = JSON.parse(e.data);
          if (j && typeof j.event === "string") handle(j.event, j.data ?? {});
          else if (j && j.status) handle("status", j);
          else soon();
        } catch {
          soon();
        }
      };
      es.onerror = () => {
        if (closed) return;
        es?.close();
        es = null;
        if (statusRef.current && TERMINAL.has(statusRef.current)) {
          stop();
          return;
        }
        setMode("polling");
        startPolling(3000);
      };
    });

    return () => {
      stop();
      if (refetchTimer.current) clearTimeout(refetchTimer.current);
    };
  }, [id, load, soon]);

  return { run, setRun, error, lines, mode, reload: load };
}
