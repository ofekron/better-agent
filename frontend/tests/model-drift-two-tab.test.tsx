import { describe, expect, it } from "vitest";
import { useEffect, useRef, useState } from "react";
import { act, render } from "@testing-library/react";
import { resolveModelDriftAction } from "../src/utils/modelDrift";

// par with frontend/src/App.tsx: session-switch effect (~L4560) + drift
// effect (~L4584), model-selector-sync portion only (cwd sync omitted —
// not implicated in this bug). Keep this choreography in lockstep with
// App.tsx if either effect's ref bookkeeping changes.
//
// tests/modelDrift.test.ts proves `resolveModelDriftAction` itself
// converges given correct inputs. It does NOT prove the real App.tsx ref
// bookkeeping (skipDriftRef / lastAppliedSelectorsSeqRef /
// lastSyncedSessionIdRef) feeds it correctly across real React effect
// scheduling — that's where the actual bug lived (two tabs, real render
// cycles, real WS-broadcast timing). This file mounts the real effect
// pair, twice, to close that gap.
function ModelSyncTab({
  sessionId,
  sessionModel,
  sessionSelectorsSeq,
  onPatch,
  onModelChange,
}: {
  sessionId: string;
  sessionModel: string;
  sessionSelectorsSeq: number;
  onPatch: (model: string) => void;
  onModelChange: (model: string) => void;
}) {
  const [model, setModel] = useState("");
  const lastSyncedSessionIdRef = useRef<string | null>(null);
  const skipDriftRef = useRef(false);
  const lastAppliedSelectorsSeqRef = useRef(0);

  useEffect(() => {
    if (sessionId !== lastSyncedSessionIdRef.current) {
      lastSyncedSessionIdRef.current = sessionId;
      setModel(sessionModel || "");
      skipDriftRef.current = true;
      lastAppliedSelectorsSeqRef.current = sessionSelectorsSeq || 0;
    }
    // Session-id-driven resync only, mirroring App.tsx's `[currentSession]`
    // dep (compared by id inside the effect, not by the whole object).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);

  useEffect(() => {
    onModelChange(model);
  }, [model, onModelChange]);

  useEffect(() => {
    const sessionSeqAtRunStart = sessionSelectorsSeq || 0;
    const lastAppliedSeq = lastAppliedSelectorsSeqRef.current;
    lastAppliedSelectorsSeqRef.current = sessionSeqAtRunStart;
    if (skipDriftRef.current) {
      skipDriftRef.current = false;
      return;
    }
    if (sessionId !== lastSyncedSessionIdRef.current) return;
    if (!model) return;
    const action = resolveModelDriftAction({
      model,
      sessionModel,
      sessionSelectorsSeq: sessionSeqAtRunStart,
      lastAppliedSelectorsSeq: lastAppliedSeq,
    });
    if (action.kind === "none") return;
    if (action.kind === "adopt") {
      skipDriftRef.current = true;
      setModel(action.model);
      return;
    }
    onPatch(action.model);
  }, [model, sessionModel, sessionSelectorsSeq, sessionId, onPatch]);

  return null;
}

const SONNET = "claude-sonnet-5";
const OPUS = "claude-opus-5[1m]";

describe("model drift — two real tabs on the same session (mounted effects)", () => {
  it("converges instead of ping-ponging when a WS broadcast lands in the other tab", () => {
    // Shared durable "session.model"/"session.selectors_seq" as both tabs'
    // WS listener would see it, and a patch counter standing in for the
    // backend PATCH endpoint (which bumps selectors_seq on every accepted
    // write — see SessionManager._bump_selectors_seq).
    let sessionModel = SONNET;
    let sessionSeq = 0;
    let patchCount = 0;
    let tabAModel = "";
    let tabBModel = "";

    function Harness({ session, seq }: { session: string; seq: number }) {
      return (
        <>
          <ModelSyncTab
            sessionId="s1"
            sessionModel={session}
            sessionSelectorsSeq={seq}
            onModelChange={(m) => { tabAModel = m; }}
            onPatch={(m) => {
              patchCount++;
              sessionModel = m;
              sessionSeq++;
            }}
          />
          <ModelSyncTab
            sessionId="s1"
            sessionModel={session}
            sessionSelectorsSeq={seq}
            onModelChange={(m) => { tabBModel = m; }}
            onPatch={(m) => {
              patchCount++;
              sessionModel = m;
              sessionSeq++;
            }}
          />
        </>
      );
    }

    const { rerender } = render(<Harness session={sessionModel} seq={sessionSeq} />);
    // Both tabs mount already agreeing with the session — no drift yet.
    expect(tabAModel).toBe(SONNET);
    expect(tabBModel).toBe(SONNET);
    expect(patchCount).toBe(0);

    // Tab A's user picks OPUS locally (simulated directly: this PATCH
    // already landed, bumping the backend's selectors_seq).
    act(() => {
      sessionModel = OPUS;
      sessionSeq = 1;
      patchCount = 0; // reset: this assignment models "Tab A's PATCH already landed"
    });
    rerender(<Harness session={sessionModel} seq={sessionSeq} />);

    // One broadcast lands in both tabs (both adopt OPUS via the advanced
    // seq). No PATCH fires from either side as a result — this is the
    // exact defect: the old code had no seq to consult and would have
    // PATCHed the stale value straight back here.
    expect(patchCount).toBe(0);
    expect(tabAModel).toBe(OPUS);
    expect(tabBModel).toBe(OPUS);

    // Re-render again with no further change (simulates any additional
    // spurious effect re-run, e.g. from an unrelated parent re-render) —
    // must stay perfectly quiescent.
    rerender(<Harness session={sessionModel} seq={sessionSeq} />);
    expect(patchCount).toBe(0);
    expect(tabAModel).toBe(OPUS);
    expect(tabBModel).toBe(OPUS);
  });

  it("still persists a genuine local model pick when the seq hasn't moved", () => {
    let sessionModel = SONNET;
    let sessionSeq = 0;
    let patchCount = 0;
    let lastPatched = "";
    let tabModel = "";
    let forceModel: ((m: string) => void) | null = null;

    function OneTab({ session, seq }: { session: string; seq: number }) {
      const [model, setModel] = useState("");
      const lastSyncedSessionIdRef = useRef<string | null>(null);
      const skipDriftRef = useRef(false);
      const lastAppliedSelectorsSeqRef = useRef(0);
      forceModel = setModel;

      useEffect(() => {
        if ("s1" !== lastSyncedSessionIdRef.current) {
          lastSyncedSessionIdRef.current = "s1";
          setModel(session || "");
          skipDriftRef.current = true;
          lastAppliedSelectorsSeqRef.current = seq || 0;
        }
        // eslint-disable-next-line react-hooks/exhaustive-deps
      }, []);

      useEffect(() => { tabModel = model; }, [model]);

      useEffect(() => {
        const sessionSeqAtRunStart = seq || 0;
        const lastAppliedSeq = lastAppliedSelectorsSeqRef.current;
        lastAppliedSelectorsSeqRef.current = sessionSeqAtRunStart;
        if (skipDriftRef.current) {
          skipDriftRef.current = false;
          return;
        }
        if (!model) return;
        const action = resolveModelDriftAction({
          model,
          sessionModel: session,
          sessionSelectorsSeq: sessionSeqAtRunStart,
          lastAppliedSelectorsSeq: lastAppliedSeq,
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
        sessionSeq++;
      }, [model, session, seq]);

      return null;
    }

    const { rerender } = render(<OneTab session={sessionModel} seq={sessionSeq} />);
    expect(tabModel).toBe(SONNET);
    expect(patchCount).toBe(0);

    // Real user interaction: pick OPUS locally, session hasn't moved.
    act(() => { forceModel!(OPUS); });
    rerender(<OneTab session={sessionModel} seq={sessionSeq} />);

    expect(patchCount).toBe(1);
    expect(lastPatched).toBe(OPUS);
  });
});
