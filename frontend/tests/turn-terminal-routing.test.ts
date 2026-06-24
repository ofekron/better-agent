import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { resolveLiveFrameSessionId, useWebSocket } from "../src/hooks/useWebSocket";
import { MockWebSocketController } from "./harness/mockWebSocket";
import type { WSEvent } from "../src/types";

/**
 * Regression: terminal WS frames (turn_complete / turn_stopped /
 * turn_detached / error) must route to the session the turn belongs to
 * (`event.data.app_session_id`), NOT the focused pane
 * (`currentAppSessionId`).
 *
 * The WS subscribes to every open pane. Before the fix these frames
 * carried no app_session_id and the handler routed by the focused pane,
 * so a turn finishing in a background pane (e.g. you sent a native
 * prompt then switched away) fired `onTurnTerminal(focusedId)` — clearing
 * the WRONG pane's `isStreaming`. The real pane stayed stuck "Running…"
 * until a manual refresh re-pulled REST.
 */
describe("useWebSocket terminal-frame routing by app_session_id", () => {
  let ctrl: MockWebSocketController;

  async function flush() {
    await act(async () => {
      await Promise.resolve();
      await new Promise((r) => setTimeout(r, 0));
      await Promise.resolve();
    });
  }

  function emit(event: WSEvent) {
    act(() => ctrl.getCurrent().deliver(event));
  }

  beforeEach(() => {
    ctrl = new MockWebSocketController();
    ctrl.install();
  });
  afterEach(() => ctrl.uninstall());

  async function mount(focused: string) {
    const onTurnTerminal = vi.fn();
    const onTurnDetached = vi.fn();
    const r = renderHook(() =>
      useWebSocket("ws://test", {
        currentAppSessionId: focused,
        onTurnTerminal,
        onTurnDetached,
      }),
    );
    await flush(); // let the socket open + currentAppSessionId effect settle
    return { onTurnTerminal, onTurnDetached, r };
  }

  it("turn_complete for a BACKGROUND session routes to THAT session, not the focused one", async () => {
    const { onTurnTerminal } = await mount("b");
    emit({ type: "turn_complete", data: { app_session_id: "a", success: true } });
    expect(onTurnTerminal).toHaveBeenCalledWith("a");
    expect(onTurnTerminal).not.toHaveBeenCalledWith("b");
  });

  it("turn_complete for the focused session routes to it", async () => {
    const { onTurnTerminal } = await mount("b");
    emit({ type: "turn_complete", data: { app_session_id: "b", success: true } });
    expect(onTurnTerminal).toHaveBeenCalledWith("b");
  });

  it("turn_complete with no app_session_id falls back to the focused session", async () => {
    const { onTurnTerminal } = await mount("b");
    emit({ type: "turn_complete", data: { success: true } });
    expect(onTurnTerminal).toHaveBeenCalledWith("b");
  });

  it("turn_stopped for a background session routes there with its stop metadata", async () => {
    const { onTurnTerminal } = await mount("b");
    emit({
      type: "turn_stopped",
      data: { app_session_id: "a", stopped_at: "2026-01-01T00:00:00", interrupted_by_msg_id: "m1" },
    });
    expect(onTurnTerminal).toHaveBeenCalledWith("a", "2026-01-01T00:00:00", "m1");
  });

  it("turn_detached for a background session routes there", async () => {
    const { onTurnDetached } = await mount("b");
    emit({ type: "turn_detached", data: { app_session_id: "a", msg_id: "x" } });
    expect(onTurnDetached).toHaveBeenCalledWith("a");
    expect(onTurnDetached).not.toHaveBeenCalledWith("b");
  });
});

describe("live todo snapshot routing", () => {
  it("routes by app_session_id before the focused session", () => {
    expect(
      resolveLiveFrameSessionId(
        { type: "todos_snapshot", data: { app_session_id: "todo-owner", session_id: "legacy" } },
        "focused",
      ),
    ).toBe("todo-owner");
  });

  it("routes legacy todos_snapshot frames by session_id before the focused session", () => {
    expect(
      resolveLiveFrameSessionId(
        { type: "todos_snapshot", data: { session_id: "todo-owner" } },
        "focused",
      ),
    ).toBe("todo-owner");
  });

  it("does not treat session_id as a generic live-frame routing key", () => {
    expect(
      resolveLiveFrameSessionId(
        { type: "turn_complete", data: { session_id: "wrong-shape" } },
        "focused",
      ),
    ).toBe("focused");
  });
});
