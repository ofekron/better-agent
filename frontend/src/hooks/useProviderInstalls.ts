import { useCallback, useEffect, useRef, useState } from "react";
import { eventBus, type BusEventMap } from "src/lib/eventBus";
import { API } from "../api";
import { subscribeProviderFrames } from "./useProviderSurfaceSocket";
import type { InstallRunWire } from "../adapter/wire";

// DUAL-SUBSCRIBED (closure 2, ADR 0007 provider/auth plane migration):
// `backend/surface_contract/descriptors.py`'s `ProviderFrame` union now
// carries `InstallProgressChanged` (`provider.install_progress_changed`,
// fired from `providers_api._broadcast_install` alongside the legacy
// `provider_install_progress`/`provider_install_finished` WS broadcasts —
// same call site, same data, both fire every time). The v2 side is added
// coverage here, not a full cutover, for one concrete, traced reason:
// `SettingsPage.tsx` mounts this hook unconditionally on first render, and
// the shared `/ws/v2/surface` "providers" socket
// (`useProviderSurfaceSocket.ts`) opens lazily (`queueMicrotask`) —
// `startInstall()` can be called (a button click) and the backend's
// `start_install()` can synchronously fire the FIRST progress fact (e.g.
// the immediate "missing prerequisite, no install path" terminal case)
// before that socket finishes opening/subscribing, which the legacy
// `eventBus`-backed WS connection (already open at app boot,
// `useWebSocket.ts`) never races. Kept dual until that startup-ordering gap
// is closed
// (e.g. `SettingsPage.tsx` awaiting `isProviderSocketOpen()` before install
// buttons become clickable) rather than accept a real dropped-first-frame
// regression. `GET /api/provider-setup/installs` (cold-start snapshot) has
// no v2 equivalent either (no ADR 0007 REST route for install runs) and
// stays legacy-only — only the LIVE push half of this hook is dual.

export type InstallRun = BusEventMap["provider_install_finished"];
export type InstallLine = InstallRun["lines"][number];

function mapInstallRunWire(run: InstallRunWire): InstallRun {
  return {
    kind: run.kind,
    label: run.label,
    command: run.command,
    state: run.state,
    lines: run.lines.map((line) => ({ s: line.stream as InstallLine["s"], t: line.text })),
    started_at: run.started_at,
    finished_at: run.finished_at,
    returncode: run.returncode,
    installed: run.installed,
    message: run.message,
  };
}

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

  // v2 counterpart: every `install_progress_changed` frame already carries
  // the FULL run snapshot (`providers_api._broadcast_install`, closure 2)
  // — a wholesale replace covers both the legacy `applyProgress`
  // (incremental line-append) and `applyFinished` (terminal replace) cases
  // in one function, since there is no partial-delta shape on this side to
  // accumulate. `onFinished` fires on the same "left running" transition
  // `applyFinished` uses.
  const applyInstallRun = useCallback((run: InstallRun) => {
    setRuns((prev) => ({ ...prev, [run.kind]: run }));
    if (run.state !== "running") onFinishedRef.current?.(run.kind);
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
    const unsubscribeV2 = subscribeProviderFrames((frame) => {
      if (frame.type === "install_progress_changed") {
        applyInstallRun(mapInstallRunWire(frame.run));
      }
    });
    return () => {
      cancelled = true;
      unsubscribeProgress();
      unsubscribeFinished();
      unsubscribeV2();
    };
  }, [applyProgress, applyFinished, applyInstallRun]);

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
