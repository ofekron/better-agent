import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, waitFor } from "@testing-library/react";

// --- hoisted eventBus capture ----------------------------------------------

const holders = vi.hoisted(() => ({
  unsubs: [] as ReturnType<typeof vi.fn>[],
  handlers: {} as Record<string, Array<(p: { session_id: string }) => void>>,
}));

vi.mock("../src/lib/eventBus", () => ({
  eventBus: {
    subscribe: (name: string, cb: (p: { session_id: string }) => void) => {
      (holders.handlers[name] ||= []).push(cb);
      const unsub = vi.fn();
      holders.unsubs.push(unsub);
      return unsub;
    },
  },
}));

vi.mock("../src/hooks/useBackButtonDismiss", () => ({
  useBackButtonDismiss: vi.fn(),
}));

vi.mock("../src/components/Icon", () => ({
  default: ({ name }: { name: string }) => <span data-icon={name} />,
}));

vi.mock("../src/components/RunDetailsModal", () => ({
  Section: ({
    title,
    subtitle,
    children,
  }: {
    title: string;
    subtitle?: string;
    children?: React.ReactNode;
  }) => (
    <div data-section={title}>
      {subtitle && <div data-subtitle>{subtitle}</div>}
      {children}
    </div>
  ),
  KV: ({ v, mono }: { k: string; v: string; mono?: boolean }) => (
    <span data-kv data-mono={mono ? "1" : "0"}>
      {v}
    </span>
  ),
  ProcessTable: ({ rows }: { rows: unknown[] }) => (
    <div data-proctable>{rows.length} rows</div>
  ),
}));

vi.mock("../src/api", () => ({ API: "http://test" }));

import { SessionDetailsPanel } from "../src/components/SessionDetailsPanel";

// --- fetch stub -------------------------------------------------------------

type FetchResp = {
  ok: boolean;
  status: number;
  statusText: string;
  text: () => Promise<string>;
  json: () => Promise<unknown>;
};

function res(body: unknown, ok = true, statusText = ""): FetchResp {
  return {
    ok,
    status: ok ? 200 : 500,
    statusText,
    text: () => Promise.resolve(typeof body === "string" ? body : JSON.stringify(body)),
    json: () => Promise.resolve(body),
  };
}

const DETAILS = {
  session_id: "s1",
  monitoring_state: "active",
  tracking_guaranteed: true,
  provenance: [
    {
      uuid: "u1",
      tool: "Bash",
      input: { command: "ls -la" },
      why: "list files",
      ts: "2024-01-01T00:00:00Z",
      msg_id: "m1",
    },
    {
      uuid: "u2",
      tool: null,
      input: { note: "x" },
      why: "",
      ts: null,
      msg_id: null,
    },
  ],
  runs: [
    {
      run_id: "r1abcdef",
      kind: "claude",
      pid: 123,
      started_at: "2024-01-01T00:00:00Z",
      processes: [{ pid: 1, state: "R", rss_kb: 10 } as never],
    },
    {
      run_id: "r2abcdef",
      kind: "agy",
      pid: null,
      started_at: "2024-01-01T00:00:00Z",
      processes: [],
    },
  ],
};

function setFetch(impl: (url: string, init?: RequestInit) => Promise<FetchResp>): void {
  globalThis.fetch = vi.fn(impl) as unknown as typeof fetch;
}

function text(container: HTMLElement): string {
  return (container.querySelector(".modal-body") ?? container).textContent ?? "";
}

// --- tests ------------------------------------------------------------------

describe("SessionDetailsPanel", () => {
  beforeEach(() => {
    holders.unsubs.length = 0;
    for (const k of Object.keys(holders.handlers)) delete holders.handlers[k];
    setFetch(async () => res(DETAILS));
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders nothing when closed", () => {
    const { container } = render(
      <SessionDetailsPanel open={false} sessionId="s1" onClose={() => {}} />,
    );
    expect(container.firstChild).toBeNull();
    // no subscriptions registered while closed
    expect(holders.handlers["session_monitoring_changed"]).toBeUndefined();
  });

  it("shows the loading placeholder before first data lands", async () => {
    let release!: () => void;
    setFetch(
      () =>
        new Promise<FetchResp>((r) => {
          release = () => r(res(DETAILS));
        }),
    );
    const { container } = render(
      <SessionDetailsPanel open={true} sessionId="s1" onClose={() => {}} />,
    );
    await waitFor(() => expect(text(container)).toContain("Loading…"));
    act(() => release());
    await waitFor(() => expect(text(container)).toContain("Active — executing"));
  });

  it("renders known state, provenance items, and process tables on success", async () => {
    const { container } = render(
      <SessionDetailsPanel open={true} sessionId="s1" onClose={() => {}} />,
    );
    await waitFor(() => expect(text(container)).toContain("Active — executing"));
    // tracking guaranteed → no best-effort warning
    expect(text(container)).not.toContain("Best-effort process tracking");
    // provenance: 2 items rendered; tool fallback "tool" for null tool
    expect(container.querySelectorAll("[data-kv]")).toHaveLength(2);
    expect(text(container)).toContain("ls -la");
    expect(text(container)).toContain("tool"); // row.tool ?? "tool"
    expect(text(container)).toContain("list files"); // row.why
    // run with processes → ProcessTable; empty run → "No live processes"
    expect(container.querySelectorAll("[data-proctable]")).toHaveLength(1);
    expect(text(container)).toContain("No live processes");
    // runs present → no "No live runs" fallback
    expect(text(container)).not.toContain("No live runs for this session");
  });

  it("covers provenance input-without-command (JSON.stringify) branch", async () => {
    const { container } = render(
      <SessionDetailsPanel open={true} sessionId="s1" onClose={() => {}} />,
    );
    await waitFor(() => expect(container.querySelectorAll("[data-kv]")).toHaveLength(2));
    // second row had no command in input → JSON of {note:"x"}
    expect(text(container)).toContain(JSON.stringify({ note: "x" }));
  });

  it("falls back to raw state + color when monitoring_state is unknown", async () => {
    setFetch(async () => res({ ...DETAILS, monitoring_state: "weird_state" }));
    const { container } = render(
      <SessionDetailsPanel open={true} sessionId="s1" onClose={() => {}} />,
    );
    await waitFor(() => expect(text(container)).toContain("weird_state"));
    // unknown → raw state shown, not a label
    expect(text(container)).not.toContain("Active — executing");
  });

  it("shows the best-effort warning when tracking is not guaranteed", async () => {
    setFetch(async () => res({ ...DETAILS, tracking_guaranteed: false }));
    const { container } = render(
      <SessionDetailsPanel open={true} sessionId="s1" onClose={() => {}} />,
    );
    await waitFor(() => expect(text(container)).toContain("Best-effort process tracking"));
  });

  it("shows the empty-provenance hint when nothing ran", async () => {
    setFetch(async () => res({ ...DETAILS, provenance: [] }));
    const { container } = render(
      <SessionDetailsPanel open={true} sessionId="s1" onClose={() => {}} />,
    );
    await waitFor(() => expect(text(container)).toContain("No tool activity recorded yet."));
  });

  it("shows the no-runs fallback when there are no live runs", async () => {
    setFetch(async () => res({ ...DETAILS, runs: [] }));
    const { container } = render(
      <SessionDetailsPanel open={true} sessionId="s1" onClose={() => {}} />,
    );
    await waitFor(() => expect(text(container)).toContain("No live runs for this session"));
    // no runs → no ProcessTable rendered
    expect(container.querySelectorAll("[data-proctable]")).toHaveLength(0);
  });

  it("renders an HTTP error from the backend", async () => {
    setFetch(async () => res("boom body", false, "Internal"));
    const { container } = render(
      <SessionDetailsPanel open={true} sessionId="s1" onClose={() => {}} />,
    );
    await waitFor(() => expect(text(container)).toContain("HTTP 500: boom body"));
  });

  it("renders a thrown-network error from fetch", async () => {
    setFetch(async () => {
      throw new Error("network down");
    });
    const { container } = render(
      <SessionDetailsPanel open={true} sessionId="s1" onClose={() => {}} />,
    );
    await waitFor(() => expect(text(container)).toContain("network down"));
  });

  it("uses statusText when the error body text() rejects (covers catch fallback)", async () => {
    globalThis.fetch = vi.fn(async () => ({
      ok: false,
      status: 502,
      statusText: "BadGateway",
      text: () => Promise.reject(new Error("unreadable")),
      json: () => Promise.resolve({}),
    })) as unknown as typeof fetch;
    const { container } = render(
      <SessionDetailsPanel open={true} sessionId="s1" onClose={() => {}} />,
    );
    await waitFor(() => expect(text(container)).toContain("HTTP 502: BadGateway"));
  });

  it("falls back to the generic message when the thrown error has none", async () => {
    setFetch(async () => {
      throw {};
    });
    const { container } = render(
      <SessionDetailsPanel open={true} sessionId="s1" onClose={() => {}} />,
    );
    await waitFor(() => expect(text(container)).toContain("Failed to load session details"));
  });

  it("refetches on a matching monitoring ping and ignores non-matching", async () => {
    const { container } = render(
      <SessionDetailsPanel open={true} sessionId="s1" onClose={() => {}} />,
    );
    await waitFor(() => expect(text(container)).toContain("Active — executing"));
    const fetchMock = globalThis.fetch as unknown as ReturnType<typeof vi.fn>;
    const callsAfterLoad = fetchMock.mock.calls.length;

    const monitoringHandler = holders.handlers["session_monitoring_changed"]![0];
    // non-matching session → no refetch
    act(() => monitoringHandler({ session_id: "other" }));
    expect(fetchMock.mock.calls.length).toBe(callsAfterLoad);
    // matching → refetch
    act(() => monitoringHandler({ session_id: "s1" }));
    await waitFor(() => expect(fetchMock.mock.calls.length).toBe(callsAfterLoad + 1));
    expect(text(container)).toContain("Active — executing");
  });

  it("subscribes to provenance pings too", async () => {
    const { container } = render(
      <SessionDetailsPanel open={true} sessionId="s1" onClose={() => {}} />,
    );
    await waitFor(() => expect(text(container)).toContain("Active — executing"));
    const provenanceHandler = holders.handlers["session_provenance_changed"]![0];
    const fetchMock = globalThis.fetch as unknown as ReturnType<typeof vi.fn>;
    const before = fetchMock.mock.calls.length;
    act(() => provenanceHandler({ session_id: "s1" }));
    await waitFor(() => expect(fetchMock.mock.calls.length).toBe(before + 1));
  });

  it("unsubscribes when toggled closed", async () => {
    const { container, rerender } = render(
      <SessionDetailsPanel open={true} sessionId="s1" onClose={() => {}} />,
    );
    await waitFor(() => expect(text(container)).toContain("Active — executing"));
    expect(holders.unsubs.length).toBeGreaterThanOrEqual(2);
    rerender(
      <SessionDetailsPanel open={false} sessionId="s1" onClose={() => {}} />,
    );
    expect(holders.unsubs.length).toBeGreaterThanOrEqual(2);
    expect(holders.unsubs.every((u) => u.mock.calls.length === 1)).toBe(true);
  });

  it("Refresh button triggers a reload and shows the busy label while pending", async () => {
    let release!: () => void;
    let first = true;
    setFetch(() => {
      if (first) {
        first = false;
        return Promise.resolve(res(DETAILS));
      }
      return new Promise<FetchResp>((r) => {
        release = () => r(res(DETAILS));
      });
    });
    const { container } = render(
      <SessionDetailsPanel open={true} sessionId="s1" onClose={() => {}} />,
    );
    await waitFor(() => expect(text(container)).toContain("Active — executing"));
    const refreshBtn = Array.from(container.querySelectorAll("button")).find((b) =>
      (b.textContent ?? "").includes("Refresh"),
    ) as HTMLButtonElement;
    expect(refreshBtn.textContent).toBe("Refresh");

    fireEvent.click(refreshBtn);
    await waitFor(() => expect(refreshBtn.textContent).toBe("Refreshing…"));
    expect(refreshBtn.disabled).toBe(true);

    act(() => release());
    await waitFor(() => expect(refreshBtn.textContent).toBe("Refresh"));
    expect(refreshBtn.disabled).toBe(false);
  });

  it("overlay close button and backdrop click call onClose; inner click does not", async () => {
    const onClose = vi.fn();
    const { container } = render(
      <SessionDetailsPanel open={true} sessionId="s1" onClose={onClose} />,
    );
    await waitFor(() => expect(text(container)).toContain("Active — executing"));

    // inner content click must not close (stopPropagation)
    fireEvent.click(container.querySelector(".modal-content")!);
    expect(onClose).not.toHaveBeenCalled();

    // explicit close button
    fireEvent.click(container.querySelector(".modal-close")!);
    expect(onClose).toHaveBeenCalledTimes(1);

    // backdrop click closes
    fireEvent.click(container.querySelector(".modal-overlay")!);
    expect(onClose).toHaveBeenCalledTimes(2);
  });
});
