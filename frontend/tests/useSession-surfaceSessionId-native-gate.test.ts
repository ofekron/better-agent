// Regression for the dual-consumer defect found while diagnosing the
// live-validation empty-native-surface finding (session d318331f-*): field
// evidence showed TWO concurrent, independent `/ws/v2/surface` connections
// for the SAME session when `ba.surface_native=1` — one from the legacy
// down-map thin client (`useSurfaceSession`, wired unconditionally in
// useSession.ts whenever `ba.surface_v2` isn't explicitly OFF, which is the
// default) and one from the new native `SurfaceStore`. Chat.tsx's own
// branch selection NEVER renders the down-map's output while native is on
// (see Chat.tsx's ternary — it always picks ChatSurfaceView/
// NativeForkSplitView instead), so the down-map hook running at the same
// time is pure redundancy: extra REST/WS traffic and a second, independent
// hydrate/resync cycle racing the native store's own for no consumer that
// ever renders.
//
// `resolveSurfaceSessionId` (useSession.ts) is the single source of truth
// for whether the down-map hook is active for the current session; this
// tests it directly. Before the fix it was `readSurfaceV2Flag() ?
// wsTargetSessionId : null` — native being on had NO effect on it, so with
// both flags on (the field run's exact configuration: ba.surface_v2 default
// ON + ba.surface_native=1) it returned `wsTargetSessionId` unchanged,
// activating the redundant consumer. That is mechanically un-arguable from
// the old expression alone (`true ? X : null` is always `X`), so this test
// documents the fixed contract going forward rather than toggling the
// source.

import { describe, it, expect } from "vitest";
import { resolveSurfaceSessionId } from "../src/hooks/useSession";

describe("resolveSurfaceSessionId", () => {
  it("disables the down-map hook when native is on, even though surface_v2 is also on", () => {
    expect(resolveSurfaceSessionId("s1", /* surfaceV2Enabled */ true, /* nativeSurfaceEnabled */ true)).toBeNull();
  });

  it("still drives the down-map hook when only surface_v2 is on (native off — pre-existing behavior)", () => {
    expect(resolveSurfaceSessionId("s1", true, false)).toBe("s1");
  });

  it("is null when surface_v2 itself is off, regardless of native", () => {
    expect(resolveSurfaceSessionId("s1", false, false)).toBeNull();
    expect(resolveSurfaceSessionId("s1", false, true)).toBeNull();
  });

  it("stays null with no target session id", () => {
    expect(resolveSurfaceSessionId(null, true, false)).toBeNull();
    expect(resolveSurfaceSessionId(null, true, true)).toBeNull();
  });
});
