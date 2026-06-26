import { useState, useCallback } from "react";
import type { Trace, TraceIndexEntry } from "../types";

import { extBackendBase } from "../extensionIds";

const traceInspectorApi = () => `${extBackendBase("traceInspector")}/traces`;

export function useTraces() {
  const [traces, setTraces] = useState<TraceIndexEntry[]>([]);
  const [currentTrace, setCurrentTrace] = useState<Trace | null>(null);
  const [loading, setLoading] = useState(false);

  const fetchTraces = useCallback(async (sessionId?: string) => {
    setLoading(true);
    try {
      const params = new URLSearchParams();
      if (sessionId) params.set("session_id", sessionId);
      const resp = await fetch(`${traceInspectorApi()}?${params}`, { credentials: "include" });
      const data = await resp.json();
      setTraces(data.traces || []);
    } catch {
      setTraces([]);
    } finally {
      setLoading(false);
    }
  }, []);

  const fetchTrace = useCallback(async (traceId: string) => {
    setLoading(true);
    try {
      const resp = await fetch(`${traceInspectorApi()}/${encodeURIComponent(traceId)}`, { credentials: "include" });
      const data = await resp.json();
      if (data.trace_id) {
        setCurrentTrace(data);
      } else {
        setCurrentTrace(null);
      }
    } catch {
      setCurrentTrace(null);
    } finally {
      setLoading(false);
    }
  }, []);

  const searchTraces = useCallback(async (query: string) => {
    setLoading(true);
    try {
      const resp = await fetch(
        `${traceInspectorApi()}/search?q=${encodeURIComponent(query)}`,
        { credentials: "include" },
      );
      const data = await resp.json();
      setTraces(data.traces || []);
    } catch {
      setTraces([]);
    } finally {
      setLoading(false);
    }
  }, []);

  return { traces, currentTrace, loading, fetchTraces, fetchTrace, searchTraces, setCurrentTrace };
}
