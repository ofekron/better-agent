import { useCallback, useEffect, useRef, useState } from "react";
import { API } from "../api";

export type InstallStream = "stdout" | "stderr";
export type InstallLine = { s: InstallStream; t: string };

export type InstallRun = {
  kind: string;
  label: string;
  command: string;
  state: "running" | "succeeded" | "failed";
  lines: InstallLine[];
  started_at: string | null;
  finished_at: string | null;
  returncode: number | null;
  installed: boolean | null;
  message: string | null;
};

type RunsMap = Record<string, InstallRun>;

type ProgressDetail =
  | { kind: string; phase: "started" }
  | { kind: string; stream: InstallStream; text: string };

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

    const onProgress = (e: Event) => {
      const d = (e as CustomEvent<ProgressDetail>).detail;
      if (d?.kind) applyProgress(d);
    };
    const onFinishedEv = (e: Event) => {
      const d = (e as CustomEvent<InstallRun>).detail;
      if (d?.kind) applyFinished(d);
    };
    window.addEventListener("provider_install_progress", onProgress);
    window.addEventListener("provider_install_finished", onFinishedEv);
    return () => {
      cancelled = true;
      window.removeEventListener("provider_install_progress", onProgress);
      window.removeEventListener("provider_install_finished", onFinishedEv);
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
