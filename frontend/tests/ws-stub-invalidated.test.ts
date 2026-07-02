import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useWebSocket } from "../src/hooks/useWebSocket";
import { MockWebSocketController } from "./harness/mockWebSocket";

describe("useWebSocket stub invalidation frames", () => {
  let ctrl: MockWebSocketController;

  async function flush() {
    await act(async () => {
      await Promise.resolve();
      await new Promise((r) => setTimeout(r, 0));
      await Promise.resolve();
    });
  }

  beforeEach(() => {
    ctrl = new MockWebSocketController();
    ctrl.install();
  });

  afterEach(() => ctrl.uninstall());

  async function mount() {
    const onStubInvalidated = vi.fn();
    const rendered = renderHook(() =>
      useWebSocket("ws://test", {
        currentAppSessionId: "focused",
        onStubInvalidated,
      }),
    );
    await flush();
    return { onStubInvalidated, rendered };
  }

  it("applies every valid batched stub invalidation and ignores malformed entries", async () => {
    const { onStubInvalidated } = await mount();
    const firstStub = { event_count: 1, last_events: [] };
    const secondStub = { event_count: 2, last_events: [{ type: "x", data: {} }] };

    act(() => {
      ctrl.getCurrent().deliver({
        type: "stub_invalidated",
        data: {
          changes: [
            { app_session_id: "s1", msg_id: "m1", stub: firstStub },
            { app_session_id: "s1", msg_id: "", stub: firstStub },
            { app_session_id: "s2", msg_id: "m2", stub: secondStub },
            { app_session_id: "s3", msg_id: "m3", stub: { event_count: 3 } },
          ],
        },
      });
    });

    expect(onStubInvalidated).toHaveBeenCalledTimes(2);
    expect(onStubInvalidated).toHaveBeenNthCalledWith(1, "s1", "m1", firstStub);
    expect(onStubInvalidated).toHaveBeenNthCalledWith(2, "s2", "m2", secondStub);
  });

  it("keeps accepting legacy single stub invalidation payloads", async () => {
    const { onStubInvalidated } = await mount();
    const stub = { event_count: 1, last_events: [] };

    act(() => {
      ctrl.getCurrent().deliver({
        type: "stub_invalidated",
        data: { app_session_id: "s1", msg_id: "m1", stub },
      });
    });

    expect(onStubInvalidated).toHaveBeenCalledTimes(1);
    expect(onStubInvalidated).toHaveBeenCalledWith("s1", "m1", stub);
  });

  it("detaches websocket handlers before closing on unmount", async () => {
    const { rendered } = await mount();
    const ws = ctrl.getCurrent();

    rendered.unmount();

    expect(ws.onopen).toBeNull();
    expect(ws.onclose).toBeNull();
    expect(ws.onerror).toBeNull();
    expect(ws.onmessage).toBeNull();
  });
});
