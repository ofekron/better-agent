import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { reduceSessionProcessing } from "../src/hooks/useSession";
import { useWebSocket } from "../src/hooks/useWebSocket";
import type { SessionProcessingUpdate } from "../src/types";
import { MockWebSocketController } from "./harness/mockWebSocket";

describe("reconcile processing state", () => {
  it("replaces stale roots with the authoritative backend snapshot", () => {
    const previous = { epoch: "a".repeat(32), revision: 1, roots: { stale: true, active: true } };
    expect(reduceSessionProcessing(previous, {
      kind: "snapshot",
      rootIds: ["active"],
      epoch: "a".repeat(32),
      revision: 2,
    })).toEqual({ epoch: "a".repeat(32), revision: 2, roots: { active: true } });
    expect(reduceSessionProcessing(previous, {
      kind: "snapshot",
      rootIds: [],
      epoch: "a".repeat(32),
      revision: 2,
    })).toEqual({ epoch: "a".repeat(32), revision: 2, roots: {} });
  });

  it("keeps complete multi-root state when older progress arrives late", () => {
    const epoch = "a".repeat(32);
    const bothActive = reduceSessionProcessing(
      { epoch, revision: 1, roots: { a: true } },
      { kind: "snapshot", rootIds: ["a", "b"], epoch, revision: 2 },
    );
    expect(reduceSessionProcessing(bothActive, {
      kind: "snapshot", rootIds: ["a"], epoch, revision: 1,
    })).toBe(bothActive);
    expect(reduceSessionProcessing(bothActive, {
      kind: "snapshot", rootIds: ["a"], epoch, revision: 3,
    }).roots).toEqual({ a: true });
  });

  it("routes valid snapshots and rejects malformed network payloads", async () => {
    const controller = new MockWebSocketController();
    controller.install();
    const epoch = "a".repeat(32);
    let state = { epoch: null as string | null, revision: 0, roots: { stale: true } };
    const rendered = renderHook(() => useWebSocket("ws://test", {
      onSessionProcessing: (update: SessionProcessingUpdate) => {
        state = reduceSessionProcessing(state, update);
      },
    }));
    await act(async () => { await Promise.resolve(); });

    act(() => {
      controller.getCurrent().deliver({
        type: "session_processing_state",
        data: { root_ids: ["active"], epoch, revision: 1 },
      });
    });
    expect(state.roots).toEqual({ active: true });

    act(() => {
      controller.getCurrent().deliver({
        type: "session_processing_state",
        data: { root_ids: ["active", 7], epoch, revision: 2 },
      });
    });
    expect(state.roots).toEqual({ active: true });

    rendered.unmount();
    controller.uninstall();
  });
});
