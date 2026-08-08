// @vitest-environment happy-dom
//
// Closure 2 (install progress v2) — the `install_progress_changed`
// dual-subscribe half of `useProviderInstalls.ts`. Isolated in its own
// file (rather than added to `useProviderInstalls.test.tsx`) with
// `useProviderSurfaceSocket` mocked: that module's `subscribeProviderFrames`
// opens a real, module-singleton `/ws/v2/surface` connection with no
// per-test reset hook wired into the existing suite's 16 tests, so
// capturing the listener directly (rather than driving it through a real
// mock WebSocket + the shared-connection singleton) is both simpler and
// avoids introducing cross-test socket-lifecycle coupling into that file.

import { afterEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { useProviderInstalls, type InstallRun } from "../src/hooks/useProviderInstalls";
import type { ProviderFrame } from "../src/adapter/wire";

type FrameListener = (frame: ProviderFrame) => void;
let capturedListener: FrameListener | null = null;

vi.mock("../src/hooks/useProviderSurfaceSocket", () => ({
  subscribeProviderFrames: (listener: FrameListener) => {
    capturedListener = listener;
    return () => {
      if (capturedListener === listener) capturedListener = null;
    };
  },
}));

vi.mock("../src/api", () => ({ API: "https://backend.test" }));

function jsonRes(body: unknown) {
  return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) };
}

function emit(frame: ProviderFrame) {
  act(() => {
    capturedListener?.(frame);
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
  capturedListener = null;
});

describe("useProviderInstalls — v2 install_progress_changed dual-subscribe", () => {
  it("applies a v2 frame as a full-snapshot replace", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonRes({ runs: {} })));
    const { result } = renderHook(() => useProviderInstalls());
    await waitFor(() => expect(capturedListener).not.toBeNull());

    emit({
      type: "install_progress_changed",
      cv: 1,
      run: {
        kind: "codex", label: "Codex", command: "npm i -g codex", state: "running",
        lines: [{ stream: "stdout", text: "installing" }],
        started_at: "t0", finished_at: null, returncode: null, installed: null, message: null,
      },
    });

    expect(result.current.runs.codex).toEqual({
      kind: "codex", label: "Codex", command: "npm i -g codex", state: "running",
      lines: [{ s: "stdout", t: "installing" }],
      started_at: "t0", finished_at: null, returncode: null, installed: null, message: null,
    } satisfies InstallRun);
  });

  it("fires onFinished when a v2 frame's state leaves running, not while running", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonRes({ runs: {} })));
    const onFinished = vi.fn();
    renderHook(() => useProviderInstalls(onFinished));
    await waitFor(() => expect(capturedListener).not.toBeNull());

    emit({
      type: "install_progress_changed", cv: 1,
      run: {
        kind: "codex", label: "Codex", command: "cmd", state: "running", lines: [],
        started_at: "t0", finished_at: null, returncode: null, installed: null, message: null,
      },
    });
    expect(onFinished).not.toHaveBeenCalled();

    emit({
      type: "install_progress_changed", cv: 2,
      run: {
        kind: "codex", label: "Codex", command: "cmd", state: "succeeded", lines: [],
        started_at: "t0", finished_at: "t1", returncode: 0, installed: true, message: null,
      },
    });
    expect(onFinished).toHaveBeenCalledWith("codex");
  });

  it("unsubscribes the v2 listener on unmount", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonRes({ runs: {} })));
    const { unmount } = renderHook(() => useProviderInstalls());
    await waitFor(() => expect(capturedListener).not.toBeNull());

    unmount();
    expect(capturedListener).toBeNull();
  });
});
