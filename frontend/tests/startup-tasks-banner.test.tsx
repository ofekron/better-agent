import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { fireEvent, waitFor } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { StartupTasksBanner } from "../src/components/StartupTasksBanner";

// Bootstrap i18n so `useTranslation` inside the banner has something
// to resolve against. Side-effecting import is intentional.
import "../src/i18n";

/** Regression lock for the "Something went wrong / u is not iterable"
 * app-blanking bug.
 *
 * Pre-fix root cause: `StartupTasksBanner` mounted unconditionally
 * inside `AppMain` during the loading-window race (initial
 * `authStatus === "loading"` falls through `App.tsx`'s `"anon"` check
 * to render `AppMain` BEFORE `checkAuth` resolves). The banner's
 * mount-time `fetch('/api/startup_tasks')` got the auth-gate's 401
 * envelope `{detail:"unauthenticated"}` from `backend/main.py`. The
 * `for (const task of arr)` inside the queued `setTasks` updater
 * threw `TypeError: ... is not iterable` ON THE NEXT RENDER (not in
 * the .then, so the surrounding `.catch` couldn't see it), escaping
 * to the top-level error boundary — every session/turn appeared "not
 * rendering."
 *
 * The fix: `Array.isArray(arr)` guard on the REST path (covers 401,
 * 500, 200-with-wrong-shape, and any future schema drift) plus a
 * symmetric `if (!task?.id) return` on the WS-delta path (blocks
 * `{task: null}` / `{task: {}}` from writing `[undefined]` into the
 * map). Both paths now refuse to feed garbage into the state.
 */

/** Mount `node` into a fresh container. */
async function mount(node: React.ReactNode) {
  const container = document.createElement("div");
  document.body.appendChild(container);
  let root: Root | null = null;
  await act(async () => {
    root = createRoot(container);
    root.render(node);
  });
  return {
    container,
    unmount: () => {
      act(() => root?.unmount());
      container.remove();
    },
  };
}

/** Minimal error boundary that captures any thrown error from its
 * subtree into a ref. Without this, an error escaping React's render
 * would either crash the test runner OR get logged silently via
 * `console.error` — neither asserts cleanly. Catching it lets us
 * inspect what (if anything) bubbled. */
class CapturingErrorBoundary extends React.Component<
  { onCatch: (err: Error) => void; children: React.ReactNode },
  { errored: boolean }
> {
  state = { errored: false };
  static getDerivedStateFromError() {
    return { errored: true };
  }
  componentDidCatch(error: Error) {
    this.props.onCatch(error);
  }
  render() {
    if (this.state.errored) return null;
    return this.props.children;
  }
}

describe("StartupTasksBanner — defends against non-array responses", () => {
  let originalFetch: typeof globalThis.fetch;
  let errorSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    originalFetch = globalThis.fetch;
    // Silence React's per-error console.error so the test output is
    // legible; we'll spy on it where it matters (Case C below).
    errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
    errorSpy.mockRestore();
    vi.restoreAllMocks();
  });

  it("Case A: 401 with {detail:'unauthenticated'} envelope does not crash", async () => {
    globalThis.fetch = vi.fn(async () =>
      ({
        ok: false,
        status: 401,
        json: async () => ({ detail: "unauthenticated" }),
      }) as unknown as Response,
    );
    const caught: Error[] = [];
    const { container, unmount } = await mount(
      <CapturingErrorBoundary onCatch={(e) => caught.push(e)}>
        <StartupTasksBanner />
      </CapturingErrorBoundary>,
    );
    // waitFor polls until the .then chain (fetch → r.json → arr-then)
    // resolves AND React commits the queued setState updater (the
    // crash window). A single `await Promise.resolve()` would flush
    // only one microtask and miss the second `.then`.
    await waitFor(() => {
      expect(container.firstChild).toBeNull();
    });
    expect(caught).toEqual([]);
    unmount();
  });

  it("Case B: 200 with non-array body ({}) does not crash", async () => {
    globalThis.fetch = vi.fn(async () =>
      ({
        ok: true,
        status: 200,
        json: async () => ({}),
      }) as unknown as Response,
    );
    const caught: Error[] = [];
    const { container, unmount } = await mount(
      <CapturingErrorBoundary onCatch={(e) => caught.push(e)}>
        <StartupTasksBanner />
      </CapturingErrorBoundary>,
    );
    await waitFor(() => {
      expect(container.firstChild).toBeNull();
    });
    expect(caught).toEqual([]);
    unmount();
  });

  it("Case C: WS delta with {task:null} or {task:{}} does not corrupt state", async () => {
    // The REST fetch is irrelevant here; return an empty array so it's
    // a no-op and we can focus on the WS path.
    globalThis.fetch = vi.fn(async () =>
      ({
        ok: true,
        status: 200,
        json: async () => [],
      }) as unknown as Response,
    );
    const caught: Error[] = [];
    const { container, unmount } = await mount(
      <CapturingErrorBoundary onCatch={(e) => caught.push(e)}>
        <StartupTasksBanner />
      </CapturingErrorBoundary>,
    );
    // Let the mount-time fetch settle first.
    await waitFor(() => {
      expect(container.firstChild).toBeNull();
    });

    // Now spy on console.error for THIS section: React surfaces
    // invalid-key warnings (e.g. `key={undefined}`) via console.error.
    // Restoring after the previous silenced spy.
    errorSpy.mockRestore();
    const liveErrorSpy = vi
      .spyOn(console, "error")
      .mockImplementation(() => {});

    // Dispatch a malformed WS delta. Pre-fix, the handler would
    // happily compute `prev[undefined] = null`, materializing an
    // `undefined`-keyed entry and (eventually, on render) emitting
    // React key warnings or surfacing a corrupt row.
    await act(async () => {
      window.dispatchEvent(
        new CustomEvent("startup_task_changed", { detail: { task: null } }),
      );
    });
    await act(async () => {
      window.dispatchEvent(
        new CustomEvent("startup_task_changed", { detail: { task: {} } }),
      );
    });

    expect(caught).toEqual([]);
    expect(container.firstChild).toBeNull();
    // No React warnings from a stray `undefined` key.
    expect(liveErrorSpy).not.toHaveBeenCalled();

    liveErrorSpy.mockRestore();
    unmount();
  });

  it("renders startup work popup as dismissible", async () => {
    globalThis.fetch = vi.fn(async () =>
      ({
        ok: true,
        status: 200,
        json: async () => [
          {
            id: "recover",
            label: "startup_tasks.recover_in_flight",
            state: "running",
            started_at: "2026-06-19T00:00:00.000Z",
            finished_at: null,
            error: null,
          },
          {
            id: "housekeeping",
            label: "startup_tasks.housekeeping",
            state: "running",
            started_at: "2026-06-19T00:00:00.000Z",
            finished_at: null,
            error: null,
          },
        ],
      }) as unknown as Response,
    );

    const { container, unmount } = await mount(<StartupTasksBanner />);
    await waitFor(() => {
      expect(container.querySelector(".startup-tasks-banner")).not.toBeNull();
    });

    expect(container.querySelectorAll(".startup-tasks-banner-row")).toHaveLength(
      2,
    );
    const close = container.querySelectorAll(".startup-tasks-banner-close");
    expect(close).toHaveLength(1);
    fireEvent.click(close[0]);

    await waitFor(() => {
      expect(container.firstChild).toBeNull();
    });
    unmount();
  });

  it("keeps the startup work indicator as a bottom popup with RTL flip", () => {
    const css = readFileSync("src/styles/globals.css", "utf8");

    expect(css).toMatch(
      /\.startup-tasks-banner\s*{[^}]*position:\s*fixed;[^}]*right:\s*24px;[^}]*bottom:\s*calc\(100px \+ env\(safe-area-inset-bottom, 0px\)\);/s,
    );
    expect(css).toMatch(
      /:dir\(rtl\)\s+\.startup-tasks-banner\s*{[^}]*right:\s*auto;[^}]*left:\s*24px;/s,
    );
  });
});
