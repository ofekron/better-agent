import { useCallback, useEffect, useRef, useState } from "react";
import { eventBus, type BusEventMap } from "src/lib/eventBus";
import { API } from "../api";

export type InstallRun = BusEventMap["provider_install_finished"];
export type InstallLine = InstallRun["lines"][number];
export type InstallStream = InstallLine["s"];

type RunsMap = Record<string, InstallRun>;

type ProgressDetail = BusEventMap["provider_install_progress"];

const cloneRuns = (r: RunsMap): RunsMap => ({ ...r });

/** Per-provider streaming-CLI-install registry.
 * Backend (`provider_setup._INSTALL_RUNS`) is authoritative; this is the
 * live frontend projection. One run per kind, multiple kinds run
 * concurrently. `onFinished(kind)` fires when a run reaches a terminal
 * state so callers can refetch setup status. */
export function useProviderInstalls(onFinished?: (kind: string) => void) {
  const [runs, setRuns] = useState<RunsMap>({});
  const onFinishedRef = useRef(onFinished);
  onFinishedRef.current = onFinished;

  const applyProgress = useCallback((d: ProgressDetail) => {
    setRuns((prev) => {
      const cur = prev[d.kind];
      // A `phase: started` ping with no existing run: the POST response
      // already seeded it, nothing to render yet.
      if ("phase" in d) {
        if (!cur) return prev;
        return { ...prev, [d.kind]: { ...cur, state: "running" } };
      }
      const base: InstallRun = cur ?? {
        kind: d.kind,
        label: d.kind,
        command: d.kind,
        state: "running",
        lines: [],
        started_at: null,
        finished_at: null,
        returncode: null,
        installed: null,
        message: null,
      };
      const lines = [...base.lines, { s: d.stream, t: d.text }].slice(-500);
      return { ...prev, [d.kind]: { ...base, state: "running", lines } };
    });
  }, []);

  const applyFinished = useCallback((run: InstallRun) => {
    setRuns((prev) => ({ ...prev, [run.kind]: run }));
    onFinishedRef.current?.(run.kind);
  }, []);

  useEffect(() => {
    let cancelled = false;
    fetch(`${API}/api/provider-setup/installs`)
      .then((r) => (r.ok ? r.json() : { runs: {} }))
      .then((body: { runs?: RunsMap }) => {
        if (!cancelled && body.runs) setRuns(cloneRuns(body.runs));
      })
      .catch(() => {});

    const onProgress = (detail: ProgressDetail) => {
      if (detail?.kind) applyProgress(detail);
    };
    const onFinished = (run: InstallRun) => {
      if (run?.kind) applyFinished(run);
    };
    const unsubscribeProgress = eventBus.subscribe(
      "provider_install_progress",
      onProgress,
    );
    const unsubscribeFinished = eventBus.subscribe(
      "provider_install_finished",
      onFinished,
    );
    return () => {
      cancelled = true;
      unsubscribeProgress();
      unsubscribeFinished();
    };
  }, [applyProgress, applyFinished]);

  const startInstall = useCallback(async (kind: string) => {
    const r = await fetch(`${API}/api/provider-setup/install`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind }),
    });
    if (!r.ok) throw new Error(await r.text());
    const run = (await r.json()) as InstallRun;
    setRuns((prev) => ({ ...prev, [kind]: run }));
  }, []);

  return { runs, startInstall };
}
