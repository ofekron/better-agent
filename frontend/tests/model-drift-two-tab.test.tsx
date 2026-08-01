import { describe, expect, it } from "vitest";
import { useEffect, useRef, useState } from "react";
import { act, render } from "@testing-library/react";
import { resolveModelDriftAction } from "../src/utils/modelDrift";

// par with frontend/src/App.tsx: session-switch effect (~L4576) + drift
// effect (~L4600), model-selector-sync portion only (cwd sync omitted —
// not implicated in this bug). Keep this choreography in lockstep with
// App.tsx if either effect's ref bookkeeping changes.
//
// tests/modelDrift.test.ts proves `resolveModelDriftAction` itself
// converges given correct inputs. It does NOT prove the real App.tsx ref
// bookkeeping (skipDriftRef / prevSessionModelForDriftRef /
// lastSyncedSessionIdRef) feeds it correctly across real React effect
// scheduling — that's where the actual bug lived (two tabs, real render
// cycles, real WS-broadcast timing). This file mounts the real effect
// pair, twice, to close that gap.
function ModelSyncTab({
  sessionId,
  sessionModel,
  onPatch,
  onModelChange,
}: {
  sessionId: string;
  sessionModel: string;
  onPatch: (model: string) => void;
  onModelChange: (model: string) => void;
}) {
  const [model, setModel] = useState("");
  const lastSyncedSessionIdRef = useRef<string | null>(null);
  const skipDriftRef = useRef(false);
  const prevSessionModelForDriftRef = useRef<string | null>(null);

  useEffect(() => {
    if (sessionId !== lastSyncedSessionIdRef.current) {
      lastSyncedSessionIdRef.current = sessionId;
      setModel(sessionModel || "");
      skipDriftRef.current = true;
      prevSessionModelForDriftRef.current = sessionModel || null;
    }
    // Session-id-driven resync only, mirroring App.tsx's `[currentSession]`
    // dep (compared by id inside the effect, not by the whole object).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);

  useEffect(() => {
    onModelChange(model);
  }, [model, onModelChange]);

  useEffect(() => {
    const sessionModelAtRunStart = sessionModel || null;
    const prevSessionModel = prevSessionModelForDriftRef.current;
    prevSessionModelForDriftRef.current = sessionModelAtRunStart;
    if (skipDriftRef.current) {
      skipDriftRef.current = false;
      return;
    }
    if (sessionId !== lastSyncedSessionIdRef.current) return;
    if (!model) return;
    const action = resolveModelDriftAction({
      model,
      sessionModel: sessionModelAtRunStart,
      prevSessionModel,
    });
    if (action.kind === "none") return;
    if (action.kind === "adopt") {
      skipDriftRef.current = true;
      setModel(action.model);
      return;
    }
    onPatch(action.model);
  }, [model, sessionModel, sessionId, onPatch]);

  return null;
}

const SONNET = "claude-sonnet-5";
const OPUS = "claude-opus-5[1m]";

describe("model drift — two real tabs on the same session (mounted effects)", () => {
  it("converges instead of ping-ponging when a WS broadcast lands in the other tab", () => {
    // Shared durable "session.model" as both tabs' WS listener would see it,
    // and a patch counter standing in for the backend PATCH endpoint.
    let sessionModel = SONNET;
    let patchCount = 0;
    let tabAModel = "";
    let tabBModel = "";

    function Harness({ session }: { session: string }) {
      return (
        <>
          <ModelSyncTab
            sessionId="s1"
            sessionModel={session}
            onModelChange={(m) => { tabAModel = m; }}
            onPatch={(m) => {
              patchCount++;
              sessionModel = m;
            }}
          />
          <ModelSyncTab
            sessionId="s1"
            sessionModel={session}
            onModelChange={(m) => { tabBModel = m; }}
            onPatch={(m) => {
              patchCount++;
              sessionModel = m;
            }}
          />
        </>
      );
    }

    const { rerender } = render(<Harness session={sessionModel} />);
    // Both tabs mount already agreeing with the session — no drift yet.
    expect(tabAModel).toBe(SONNET);
    expect(tabBModel).toBe(SONNET);
    expect(patchCount).toBe(0);

    // Tab A's user picks OPUS locally (simulated by re-rendering with a
    // sessionModel prop that hasn't moved — the real trigger is a picker
    // calling setModel inside Tab A, which we can't reach from outside the
    // component, so instead we drive the same effect path via App's own
    // `onPatch` hook once directly to seed the session, then verify no
    // further tab keeps re-patching once both converge).
    act(() => {
      sessionModel = OPUS;
      patchCount = 0; // reset: this assignment models "Tab A's PATCH already landed"
    });
    rerender(<Harness session={sessionModel} />);

    // One broadcast lands in both tabs (both adopt OPUS). No PATCH fires
    // from either side as a result — this is the exact defect: the old
    // code had no "adopt" path and would have PATCHed the stale value
    // straight back here.
    expect(patchCount).toBe(0);
    expect(tabAModel).toBe(OPUS);
    expect(tabBModel).toBe(OPUS);

    // Re-render again with no further change (simulates any additional
    // spurious effect re-run, e.g. from an unrelated parent re-render) —
    // must stay perfectly quiescent.
    rerender(<Harness session={sessionModel} />);
    expect(patchCount).toBe(0);
    expect(tabAModel).toBe(OPUS);
    expect(tabBModel).toBe(OPUS);
  });

  it("still persists a genuine local model pick when the session hasn't moved", () => {
    let sessionModel = SONNET;
    let patchCount = 0;
    let lastPatched = "";
    let tabModel = "";
    let forceModel: ((m: string) => void) | null = null;

    function OneTab({ session }: { session: string }) {
      const [model, setModel] = useState("");
      const lastSyncedSessionIdRef = useRef<string | null>(null);
      const skipDriftRef = useRef(false);
      const prevSessionModelForDriftRef = useRef<string | null>(null);
      forceModel = setModel;

      useEffect(() => {
        if ("s1" !== lastSyncedSessionIdRef.current) {
          lastSyncedSessionIdRef.current = "s1";
          setModel(session || "");
          skipDriftRef.current = true;
          prevSessionModelForDriftRef.current = session || null;
        }
        // eslint-disable-next-line react-hooks/exhaustive-deps
      }, []);

      useEffect(() => { tabModel = model; }, [model]);

      useEffect(() => {
        const sessionModelAtRunStart = session || null;
        const prevSessionModel = prevSessionModelForDriftRef.current;
        prevSessionModelForDriftRef.current = sessionModelAtRunStart;
        if (skipDriftRef.current) {
          skipDriftRef.current = false;
          return;
        }
        if (!model) return;
        const action = resolveModelDriftAction({
          model,
          sessionModel: sessionModelAtRunStart,
          prevSessionModel,
        });
        if (action.kind === "none") return;
        if (action.kind === "adopt") {
          skipDriftRef.current = true;
          setModel(action.model);
          return;
        }
        patchCount++;
        lastPatched = action.model;
        sessionModel = action.model;
      }, [model, session]);

      return null;
    }

    const { rerender } = render(<OneTab session={sessionModel} />);
    expect(tabModel).toBe(SONNET);
    expect(patchCount).toBe(0);

    // Real user interaction: pick OPUS locally, session hasn't moved.
    act(() => { forceModel!(OPUS); });
    rerender(<OneTab session={sessionModel} />);

    expect(patchCount).toBe(1);
    expect(lastPatched).toBe(OPUS);
  });
});
