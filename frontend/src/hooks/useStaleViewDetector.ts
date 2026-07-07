/**
 * useStaleViewDetector — proactive, real-time stale-view detection.
 *
 * When running a DEBUG-MODE BA instance (see `debugFlags.ts`), this hook
 * continuously compares the RENDERED sessions chat panel against the
 * CANONICAL in-memory session and surfaces any mismatch the moment it
 * happens:
 *   - logs a grouped `[stale-view]` console error with the structured
 *     report (so it interleaves with the existing `[stale-dbg]` merge
 *     logs for root-causing),
 *   - dispatches a `better-agent:stale-view` window event (so a devtools
 *     overlay / e2e test / TestApe can react),
 *   - records the last report on `window.__betterAgentStaleView` for
 *     ad-hoc inspection from the console (`.check()`, `.last`, `.history`).
 *
 * In a NON-debug instance the hook is completely inert: no timers, no DOM
 * reads, no `window` surface — zero overhead in production.
 *
 * Trigger model (real-time without thrashing):
 *   - a debounced check fires shortly after any relevant input settles
 *     (session content changed, streaming toggled, WS reconnected),
 *   - a low-frequency safety-net interval catches drift that produced no
 *     React update,
 *   - checks are SKIPPED while a turn is streaming (canonical tree is
 *     legitimately mid-mutation) and re-run once streaming ends.
 */

import { useCallback, useEffect, useMemo, useRef } from "react";

import type { Session } from "src/types";
import { isDebugFeature } from "src/lib/debugFlags";
import {
  compareRenderedTreeToSession,
  sessionIsStreaming,
  type RenderedTree,
  type StaleViewReport,
} from "src/lib/staleViewDetector";
import { sessionMessageCount } from "src/lib/sessionMessageCount";

const DEBUG_TOKEN = "stale-view";
const DEBOUNCE_MS = 400;
const SAFETY_INTERVAL_MS = 5_000;
const HISTORY_LIMIT = 50;

interface StaleViewGlobal {
  enabled: boolean;
  last: StaleViewReport | null;
  history: StaleViewReport[];
  check: () => StaleViewReport | null;
}

declare global {
  interface Window {
    __betterAgentStaleView?: StaleViewGlobal;
  }
}

// `window.__betterAgentTestApe` is declared (with its own return type) in
// testapeConsumer.ts; re-declaring it here would clash. Read it through a
// narrow local type instead.
type TestApeExtractor = {
  extractVisibleChatPanelTree?: () => RenderedTree;
};

interface Options {
  currentSession: Session | null;
  connected: boolean;
}

export function useStaleViewDetector({ currentSession, connected }: Options): void {
  const enabled = useMemo(() => isDebugFeature(DEBUG_TOKEN), []);
  const sessionRef = useRef<Session | null>(currentSession);
  sessionRef.current = currentSession;
  const historyRef = useRef<StaleViewReport[]>([]);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  const runCheck = useCallback((): StaleViewReport | null => {
    if (!enabled) return null;
    if (typeof window === "undefined") return null;
    const session = sessionRef.current;

    // Skip while streaming — the canonical tree is mid-mutation and the
    // DOM legitimately lags by a frame; a check here would false-positive.
    if (sessionIsStreaming(session)) {
      return null;
    }

    const extractor = (window.__betterAgentTestApe as TestApeExtractor | undefined)
      ?.extractVisibleChatPanelTree;
    let tree: RenderedTree | null = null;
    if (typeof extractor === "function") {
      try {
        tree = extractor();
      } catch (err) {
        // The extractor threw — surface it, don't crash the app.
        // eslint-disable-next-line no-console
        console.error("[stale-view] extractor threw", err);
        tree = null;
      }
    }

    const report = compareRenderedTreeToSession(tree, session);

    const g = window.__betterAgentStaleView;
    if (g) {
      g.last = report;
      if (!report.skipped) {
        historyRef.current.push(report);
        if (historyRef.current.length > HISTORY_LIMIT) {
          historyRef.current.splice(0, historyRef.current.length - HISTORY_LIMIT);
        }
        g.history = historyRef.current;
      }
    }

    if (!report.ok && !report.skipped) {
      /* eslint-disable no-console */
      console.groupCollapsed(
        `%c[stale-view] MISMATCH in session ${String(report.session_id).slice(0, 8)} — ${report.mismatches.length} issue(s)`,
        "color:#ff5555;font-weight:bold",
      );
      for (const m of report.mismatches) {
        console.error(`[stale-view] (${m.kind}) ${m.detail}`);
      }
      console.info("[stale-view] full report", report);
      console.groupEnd();
      /* eslint-enable no-console */
      try {
        window.dispatchEvent(
          new CustomEvent("better-agent:stale-view", { detail: report }),
        );
      } catch {
        /* CustomEvent unsupported in some test envs */
      }
    }
    return report;
  }, [enabled]);

  const scheduleCheck = useCallback(() => {
    if (!enabled) return;
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      // Two rAFs so the check runs after React has committed and the
      // browser has painted the latest DOM.
      const raf = typeof requestAnimationFrame === "function" ? requestAnimationFrame : (cb: FrameRequestCallback) => setTimeout(() => cb(0), 16);
      raf(() => raf(() => runCheck()));
    }, DEBOUNCE_MS);
  }, [enabled, runCheck]);

  // Install the window inspection surface once.
  useEffect(() => {
    if (!enabled || typeof window === "undefined") return;
    const g: StaleViewGlobal = {
      enabled: true,
      last: null,
      history: historyRef.current,
      check: () => runCheck(),
    };
    window.__betterAgentStaleView = g;
    // eslint-disable-next-line no-console
    console.info(
      "%c[stale-view] proactive stale-view detection ON — window.__betterAgentStaleView.check()",
      "color:#7b68ee",
    );
    return () => {
      if (window.__betterAgentStaleView === g) {
        delete window.__betterAgentStaleView;
      }
    };
  }, [enabled, runCheck]);

  // Fingerprint of the canonical state that should be reflected in the
  // panel. When it changes, re-check. Cheap to compute; avoids deep diff.
  const fingerprint = useMemo(() => {
    const parts: string[] = [];
    const visit = (node: Session | null) => {
      if (!node) return;
      const last = node.messages?.[node.messages.length - 1];
      parts.push(
        `${node.id}:${sessionMessageCount(node)}:${node.messages?.length ?? 0}:${last?.id ?? ""}:${last?.seq ?? ""}:${last?.isStreaming ? 1 : 0}`,
      );
      for (const f of node.forks || []) visit(f);
    };
    visit(currentSession);
    return parts.join("|");
  }, [currentSession]);

  useEffect(() => {
    if (!enabled) return;
    scheduleCheck();
  }, [enabled, fingerprint, connected, scheduleCheck]);

  // Safety-net interval catches drift that produced no React update.
  useEffect(() => {
    if (!enabled || typeof window === "undefined") return;
    const id = setInterval(() => runCheck(), SAFETY_INTERVAL_MS);
    return () => clearInterval(id);
  }, [enabled, runCheck]);

  // Cleanup pending debounce on unmount.
  useEffect(() => {
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
  }, []);
}
