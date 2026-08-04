// @vitest-environment happy-dom

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";

import {
  useProviderInstalls,
  type InstallRun,
} from "../src/hooks/useProviderInstalls";
import { eventBus } from "../src/lib/eventBus";

vi.mock("../src/api", () => ({ API: "https://backend.test" }));

const INSTALLS_URL = "https://backend.test/api/provider-setup/installs";
const INSTALL_URL = "https://backend.test/api/provider-setup/install";

function jsonRes(body: unknown, ok = true) {
  return {
    ok,
    status: ok ? 200 : 500,
    json: async () => body,
    text: async () => (typeof body === "string" ? body : JSON.stringify(body)),
  };
}

function makeRun(over: Partial<InstallRun> = {}): InstallRun {
  return {
    kind: "claude",
    label: "Claude",
    command: "install.sh",
    state: "running",
    lines: [],
    started_at: null,
    finished_at: null,
    returncode: null,
    installed: null,
    message: null,
    ...over,
  };
}

function publishProgress(detail: unknown) {
  act(() => {
    eventBus.publish("provider_install_progress", detail);
  });
}

function publishFinished(detail: unknown) {
  act(() => {
    eventBus.publish("provider_install_finished", detail);
  });
}

beforeEach(() => {
  // Default: empty installs snapshot so the mount-time fetch resolves cleanly.
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).includes("/installs")) return jsonRes({ runs: {} });
      return jsonRes({});
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("useProviderInstalls", () => {
  it("seeds runs from the initial installs snapshot", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonRes({ runs: { claude: makeRun() } })),
    );

    const { result } = renderHook(() => useProviderInstalls());

    await waitFor(() => expect(result.current.runs.claude).toBeDefined());
    expect(result.current.runs.claude).toEqual(makeRun());
  });

  it("treats a non-ok installs response as an empty registry", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: false, status: 500 })),
    );

    const { result } = renderHook(() => useProviderInstalls());

    // No runs populate, and no throw escapes the hook.
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(INSTALLS_URL));
    expect(result.current.runs).toEqual({});
  });

  it("swallows a rejected installs fetch without throwing", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Promise.reject(new Error("net"))));

    const { result } = renderHook(() => useProviderInstalls());

    await waitFor(() => expect(fetch).toHaveBeenCalledWith(INSTALLS_URL));
    expect(result.current.runs).toEqual({});
  });

  it("ignores a `phase: started` ping that has no existing run", async () => {
    const { result } = renderHook(() => useProviderInstalls());
    await waitFor(() => expect(fetch).toHaveBeenCalled());

    publishProgress({ kind: "codex", phase: "started" });

    expect(result.current.runs).toEqual({});
  });

  it("marks an existing run as running on a `phase: started` ping", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonRes({ runs: { claude: makeRun({ state: "succeeded" }) } }),
      ),
    );

    const { result } = renderHook(() => useProviderInstalls());
    await waitFor(() => expect(result.current.runs.claude).toBeDefined());

    publishProgress({ kind: "claude", phase: "started" });

    expect(result.current.runs.claude!.state).toBe("running");
  });

  it("creates a run from a stream line when none exists", async () => {
    const { result } = renderHook(() => useProviderInstalls());
    await waitFor(() => expect(fetch).toHaveBeenCalled());

    publishProgress({ kind: "codex", stream: "stdout", text: "hello" });

    const run = result.current.runs.codex!;
    expect(run.state).toBe("running");
    expect(run.label).toBe("codex");
    expect(run.command).toBe("codex");
    expect(run.lines).toEqual([{ s: "stdout", t: "hello" }]);
  });

  it("appends stream lines to an existing run", async () => {
    const { result } = renderHook(() => useProviderInstalls());
    await waitFor(() => expect(fetch).toHaveBeenCalled());

    publishProgress({ kind: "codex", stream: "stdout", text: "one" });
    publishProgress({ kind: "codex", stream: "stderr", text: "two" });

    expect(result.current.runs.codex!.lines).toEqual([
      { s: "stdout", t: "one" },
      { s: "stderr", t: "two" },
    ]);
  });

  it("caps the line buffer at the most recent 500 lines", async () => {
    const { result } = renderHook(() => useProviderInstalls());
    await waitFor(() => expect(fetch).toHaveBeenCalled());

    publishProgress({ kind: "codex", stream: "stdout", text: "head" });
    act(() => {
      for (let i = 1; i <= 501; i++) {
        eventBus.publish("provider_install_progress", {
          kind: "codex",
          stream: "stdout",
          text: `l${i}`,
        });
      }
    });

    const lines = result.current.runs.codex!.lines;
    expect(lines).toHaveLength(500);
    // The earliest "head" line was evicted; the buffer keeps the latest 500.
    expect(lines[0]).toEqual({ s: "stdout", t: "l2" });
    expect(lines[499]).toEqual({ s: "stdout", t: "l501" });
  });

  it("ignores progress events that carry no kind", async () => {
    const { result } = renderHook(() => useProviderInstalls());
    await waitFor(() => expect(fetch).toHaveBeenCalled());

    publishProgress({ stream: "stdout", text: "orphan" });

    expect(result.current.runs).toEqual({});
  });

  it("projects a terminal run and fires onFinished(kind)", async () => {
    const onFinished = vi.fn();
    const { result } = renderHook(() => useProviderInstalls(onFinished));
    await waitFor(() => expect(fetch).toHaveBeenCalled());

    const terminal = makeRun({
      state: "failed",
      returncode: 1,
      message: "boom",
      finished_at: "2026-01-01T00:00:00Z",
    });
    publishFinished(terminal);

    expect(result.current.runs.claude).toEqual(terminal);
    expect(onFinished).toHaveBeenCalledWith("claude");
  });

  it("ignores a finished event without a kind and does not fire onFinished", async () => {
    const onFinished = vi.fn();
    const { result } = renderHook(() => useProviderInstalls(onFinished));
    await waitFor(() => expect(fetch).toHaveBeenCalled());

    publishFinished({ state: "failed", returncode: 1 });

    expect(onFinished).not.toHaveBeenCalled();
    expect(result.current.runs).toEqual({});
  });

  it("always invokes the latest onFinished callback", async () => {
    const cb1 = vi.fn();
    const cb2 = vi.fn();
    const { result, rerender } = renderHook(
      ({ cb }) => useProviderInstalls(cb),
      { initialProps: { cb: cb1 } },
    );
    await waitFor(() => expect(fetch).toHaveBeenCalled());
    rerender({ cb: cb2 });

    publishFinished(makeRun({ kind: "claude", state: "succeeded" }));

    expect(cb2).toHaveBeenCalledWith("claude");
    expect(cb1).not.toHaveBeenCalled();
  });

  it("startInstall seeds the run from the POST response", async () => {
    const seeded = makeRun({ state: "running", started_at: "2026-01-01" });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        if (String(input).includes("/install")) {
          // Assert the request shape as we go.
          expect(init?.method).toBe("POST");
          expect(JSON.parse(String(init?.body))).toEqual({ kind: "claude" });
          return jsonRes(seeded);
        }
        return jsonRes({ runs: {} });
      }),
    );

    const { result } = renderHook(() => useProviderInstalls());
    await waitFor(() => expect(fetch).toHaveBeenCalled());

    await act(async () => {
      await result.current.startInstall("claude");
    });

    expect(result.current.runs.claude).toEqual(seeded);
  });

  it("startInstall throws with the response text on failure", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        if (String(input).includes("/install")) return jsonRes("install denied", false);
        return jsonRes({ runs: {} });
      }),
    );

    const { result } = renderHook(() => useProviderInstalls());
    await waitFor(() => expect(fetch).toHaveBeenCalled());

    await act(async () => {
      await expect(result.current.startInstall("claude")).rejects.toThrow(
        "install denied",
      );
    });
    expect(result.current.runs).toEqual({});
  });

  it("unsubscribes from install projections on unmount", async () => {
    const { unmount, result } = renderHook(() => useProviderInstalls());
    await waitFor(() => expect(fetch).toHaveBeenCalled());

    unmount();

    // After unmount, a late progress event must not mutate state or throw.
    publishProgress({ kind: "codex", stream: "stdout", text: "late" });
    expect(result.current.runs).toEqual({});
  });

  it("ignores an in-flight installs fetch that resolves after unmount", async () => {
    let resolveGet!: (v: unknown) => void;
    vi.stubGlobal(
      "fetch",
      vi.fn(
        () =>
          new Promise((res) => {
            resolveGet = res;
          }),
      ),
    );
    const errSpy = vi.spyOn(console, "error").mockImplementation(() => {});

    const { unmount } = renderHook(() => useProviderInstalls());
    unmount();
    // Resolve only after the component is gone — the cancel guard must skip
    // the state update and emit no React unmounted-component warning.
    await act(async () => {
      resolveGet(jsonRes({ runs: { claude: makeRun() } }));
      await Promise.resolve();
    });

    expect(errSpy).not.toHaveBeenCalled();
    errSpy.mockRestore();
  });
});
