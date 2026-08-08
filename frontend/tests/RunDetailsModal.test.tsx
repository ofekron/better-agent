import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, fireEvent, waitFor } from "@testing-library/react";

// Unit-tier isolation: pin the API base and neutralize the history-manipulating
// back-button hook so this slice exercises RunDetailsModal's own branching.
vi.mock("../src/api", () => ({ API: "https://backend.test" }));
vi.mock("../src/hooks/useBackButtonDismiss", () => ({
  useBackButtonDismiss: () => {},
}));

import {
  RunDetailsModal,
  fmtMem,
  Section,
  KV,
  ProcessTable,
  type ProcessEntry,
} from "../src/components/RunDetailsModal";

/** Build a fetch Response-like object without relying on a real Response impl. */
function resp(opts: {
  ok: boolean;
  status: number;
  statusText?: string;
  body?: string;
  data?: unknown;
  textReject?: boolean;
}) {
  return {
    ok: opts.ok,
    status: opts.status,
    statusText: opts.statusText ?? "",
    text: () =>
      opts.textReject
        ? Promise.reject(new Error("read failed"))
        : Promise.resolve(opts.body ?? ""),
    json: () => Promise.resolve(opts.data),
  };
}

// ---- v2 (GET /api/v2/surface/runs/{run_id}) fixture ----------------------
const v2Detail = {
  kind: "ok",
  snapshot_identity: { incarnation: "inc-1", render_rev: 1, hist_rev: 1 },
  summary: {
    run_id: "run-1",
    session_id: "s1",
    turn_id: "t1",
    provider_id: "claude",
    runner: "native",
    phase: "running",
    started_at: 1_700_000_000,
    last_heartbeat_at: 1_700_000_005,
    startup: null,
  },
  entries: [
    { key: "run.detail.run_id", value_kind: "text", value: "run-1" },
    { key: "run.detail.session_id", value_kind: "text", value: "s1" },
    { key: "run.detail.started_at", value_kind: "text", value: 1_700_000_000 },
    { key: "run.detail.success", value_kind: "text", value: null },
    { key: "run.detail.turn_id", value_kind: "ref", value: "t1" },
    { key: "run.detail.provider_id", value_kind: "ref", value: "claude" },
    { key: "run.detail.runner", value_kind: "text", value: "native" },
    { key: "run.detail.heartbeat_silence_seconds", value_kind: "duration", value: 3.2 },
  ],
};

// ---- legacy (GET /api/sessions/{id}/runs/{run_id}/details) fixture -------
const fullDetails = {
  run_id: "run-1",
  app_session_id: "app-1",
  kind: "manager",
  target_message_id: "msg-9",
  delegation_id: "deleg-1",
  pid: 1234,
  started_at: "2025-01-01T00:00:00Z",
  last_event_at: "2025-01-01T00:01:00Z",
  provider_kind: "claude",
  startup_phase: "ready",
  startup_expected_activity: "streaming",
  startup_phase_started_at: "2025-01-01T00:00:01Z",
  startup_silence_threshold_seconds: 30,
  stalled_at: null,
  last_activity_at: "2025-01-01T00:00:30Z",
  last_activity_kind: "assistant",
  provider: {
    provider_kind: "claude",
    mode: "manager",
    session_id: "sess-1",
    jsonl_path: "/tmp/x.jsonl",
    run_dir: "/tmp/run",
    cancelled: false,
    popen_alive: true,
    popen_pid: 555,
  },
  processes: [] as ProcessEntry[],
};

const sampleProcess: ProcessEntry = {
  pid: 1,
  ppid: 2,
  stat: "R",
  state_desc: "running",
  cpu_percent: 12.345,
  rss_kb: 2048,
  elapsed: "00:01:00",
  command: "node worker.js",
  alive: true,
};

function isV2Url(url: string): boolean {
  return url.includes("/api/v2/surface/runs/");
}

/** Routes the shared global `fetch` mock by URL — v2 first (component
 * fetches it first), legacy second — matching RunDetailsModal's own
 * `load()` call order. */
function routedFetchMock(
  v2: { ok: boolean; status?: number; data?: unknown; body?: string; textReject?: boolean },
  legacy: { ok: boolean; status?: number; data?: unknown; body?: string; textReject?: boolean },
) {
  return vi.fn((url: string) => {
    if (isV2Url(url)) {
      return Promise.resolve(
        resp({ ok: v2.ok, status: v2.status ?? (v2.ok ? 200 : 500), data: v2.data, body: v2.body, textReject: v2.textReject }),
      );
    }
    return Promise.resolve(
      resp({ ok: legacy.ok, status: legacy.status ?? (legacy.ok ? 200 : 404), data: legacy.data, body: legacy.body, textReject: legacy.textReject }),
    );
  });
}

function defaultFetchMock() {
  return routedFetchMock({ ok: true, data: v2Detail }, { ok: true, data: fullDetails });
}

beforeEach(() => {
  vi.stubGlobal("fetch", defaultFetchMock());
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("fmtMem", () => {
  it("em-dash for non-positive", () => {
    expect(fmtMem(0)).toBe("—");
    expect(fmtMem(-10)).toBe("—");
  });
  it("KB below 1 MiB", () => {
    expect(fmtMem(512)).toBe("512 KB");
    expect(fmtMem(1023)).toBe("1023 KB");
  });
  it("MB between 1 MiB and 1 GiB", () => {
    expect(fmtMem(2048)).toBe("2.0 MB");
    expect(fmtMem(1048575)).toBe("1024.0 MB");
  });
  it("GB at and above 1 GiB", () => {
    expect(fmtMem(1048576)).toBe("1.00 GB");
    expect(fmtMem(1572864)).toBe("1.50 GB");
  });
});

function fetchMock() {
  return fetch as unknown as ReturnType<typeof vi.fn>;
}

describe("RunDetailsModal — open/close + dual-source fetching (v2 primary, legacy fallback)", () => {
  it("renders nothing when closed", () => {
    const { container } = render(
      <RunDetailsModal open={false} sessionId="s1" runId="r1" onClose={() => {}} />
    );
    expect(container.firstChild).toBeNull();
    expect(fetchMock()).not.toHaveBeenCalled();
  });

  it("fetches v2 first, then legacy, and renders the legacy kind in the title", async () => {
    const { container } = render(
      <RunDetailsModal open={true} sessionId="s 1" runId="r/1" onClose={() => {}} />
    );
    await waitFor(() => {
      expect(container.textContent).toContain("run-1");
    });
    expect(fetchMock()).toHaveBeenCalledTimes(2);
    // v2 call first — /runs/{run_id}, no session in the path.
    expect(fetchMock().mock.calls[0][0]).toBe(
      "https://backend.test/api/v2/surface/runs/r%2F1"
    );
    // legacy call second — session-scoped diagnostics route, unchanged shape.
    expect(fetchMock().mock.calls[1][0]).toBe(
      "https://backend.test/api/sessions/s%201/runs/r%2F1/details"
    );
    // kind still comes from the legacy fetch (v2 RunSummary has no `kind`).
    expect(container.querySelector("h2")!.textContent).toContain("manager");
  });

  it("shows the loading state before either fetch resolves", async () => {
    let resolveV2: ((v: unknown) => void) | undefined;
    let resolveLegacy: ((v: unknown) => void) | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        if (isV2Url(url)) return new Promise((r) => { resolveV2 = r; });
        return new Promise((r) => { resolveLegacy = r; });
      })
    );
    const { container } = render(
      <RunDetailsModal open={true} sessionId="s1" runId="r1" onClose={() => {}} />
    );
    const refresh = container.querySelector(".btn-secondary") as HTMLButtonElement;
    expect(refresh.disabled).toBe(true);
    expect(container.textContent).not.toContain("run-1");

    // The two fetches are sequential (v2 first, legacy second — see
    // RunDetailsModal's own `load()`), so the legacy `fetch()` call, and
    // therefore `resolveLegacy`'s assignment, only happens after the v2
    // promise's continuation runs.
    resolveV2!(resp({ ok: true, status: 200, data: v2Detail }));
    await waitFor(() => expect(resolveLegacy).toBeDefined());
    resolveLegacy!(resp({ ok: true, status: 200, data: fullDetails }));
    await waitFor(() => {
      expect(container.textContent).toContain("run-1");
    });
    expect(
      (container.querySelector(".btn-secondary") as HTMLButtonElement).disabled
    ).toBe(false);
  });

  it("v2 non-ok response surfaces the error banner (real HTTP-status message); legacy section still renders", async () => {
    vi.stubGlobal(
      "fetch",
      routedFetchMock(
        { ok: false, status: 500, body: "boom" },
        { ok: true, data: fullDetails },
      ),
    );
    const { container } = render(
      <RunDetailsModal open={true} sessionId="s1" runId="r1" onClose={() => {}} />
    );
    await waitFor(() => {
      expect(container.textContent).toContain("HTTP 500: boom");
    });
    // Legacy section is independent of the v2 failure — never silently
    // dropped by an unrelated v2 error. (No `v2Detail` section — and thus
    // no `run_id` value — renders here, since v2 failed; the legacy
    // section's own pid field is the right thing to wait on instead.)
    await waitFor(() => expect(container.textContent).toContain("1234"));
    expect(container.textContent).toContain("runDetails.legacyDiagnostics");
  });

  it("legacy 404 (run not active in turn_manager) is silent — only the v2 section renders", async () => {
    vi.stubGlobal(
      "fetch",
      routedFetchMock({ ok: true, data: v2Detail }, { ok: false, status: 404, body: "run not found" }),
    );
    const { container } = render(
      <RunDetailsModal open={true} sessionId="s1" runId="r1" onClose={() => {}} />
    );
    await waitFor(() => expect(container.textContent).toContain("run-1"));
    // No error banner from the (expected) legacy 404.
    expect(container.textContent).not.toContain("run not found");
    expect(container.textContent).not.toContain("Failed to load");
    // No legacy-only content (pid never appears in the fixture's v2 side).
    expect(container.textContent).not.toContain("1234");
    // Title has no kind suffix (legacy absent).
    expect(container.querySelector("h2")!.textContent).not.toContain("manager");
  });

  it("refresh button re-triggers both fetches", async () => {
    const { container } = render(
      <RunDetailsModal open={true} sessionId="s1" runId="r1" onClose={() => {}} />
    );
    await waitFor(() => expect(container.textContent).toContain("run-1"));
    fireEvent.click(container.querySelector(".btn-secondary")!);
    await waitFor(() => expect(fetchMock()).toHaveBeenCalledTimes(4));
  });
});

describe("RunDetailsModal — close interactions", () => {
  async function renderOpen(onClose: () => void) {
    const utils = render(
      <RunDetailsModal open={true} sessionId="s1" runId="r1" onClose={onClose} />
    );
    await waitFor(() => expect(utils.container.textContent).toContain("run-1"));
    return utils;
  }

  it("overlay click calls onClose", async () => {
    const onClose = vi.fn();
    const { container } = await renderOpen(onClose);
    fireEvent.click(container.querySelector(".modal-overlay")!);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("header close button calls onClose", async () => {
    const onClose = vi.fn();
    const { container } = await renderOpen(onClose);
    fireEvent.click(container.querySelector(".modal-close")!);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("footer close button calls onClose", async () => {
    const onClose = vi.fn();
    const { container } = await renderOpen(onClose);
    fireEvent.click(container.querySelector(".btn-primary")!);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("clicking inside the content does not close (stopPropagation)", async () => {
    const onClose = vi.fn();
    const { container } = await renderOpen(onClose);
    fireEvent.click(container.querySelector(".modal-content")!);
    expect(onClose).not.toHaveBeenCalled();
  });
});

describe("RunDetailsModal — v2 generic entries rendering", () => {
  it("renders every v2 entry generically (key stripped of the run.detail. prefix, value per value_kind)", async () => {
    const { container } = render(
      <RunDetailsModal open={true} sessionId="s1" runId="r1" onClose={() => {}} />
    );
    await waitFor(() => expect(container.textContent).toContain("run-1"));
    const text = container.textContent!;
    expect(text).toContain("phase");
    expect(text).toContain("running"); // summary.phase
    expect(text).toContain("turn_id");
    expect(text).toContain("t1"); // REF value
    expect(text).toContain("heartbeat_silence_seconds");
    expect(text).toContain("3.2s"); // DURATION formatting
  });

  it("a future backend-added entry key renders with zero client changes", async () => {
    const withNewField = {
      ...v2Detail,
      entries: [
        ...v2Detail.entries,
        { key: "run.detail.some_future_field", value_kind: "count", value: 42 },
      ],
    };
    vi.stubGlobal(
      "fetch",
      routedFetchMock({ ok: true, data: withNewField }, { ok: true, data: fullDetails }),
    );
    const { container } = render(
      <RunDetailsModal open={true} sessionId="s1" runId="r1" onClose={() => {}} />
    );
    await waitFor(() => expect(container.textContent).toContain("run-1"));
    expect(container.textContent).toContain("some_future_field");
    expect(container.textContent).toContain("42");
  });

  it("null entry values render as an em-dash regardless of value_kind", async () => {
    const { container } = render(
      <RunDetailsModal open={true} sessionId="s1" runId="r1" onClose={() => {}} />
    );
    await waitFor(() => expect(container.textContent).toContain("run-1"));
    // run.detail.success is null in the fixture.
    const kvRows = Array.from(container.querySelectorAll("div")).filter(
      (el) => el.textContent === "success",
    );
    expect(kvRows.length).toBeGreaterThan(0);
  });
});

describe("RunDetailsModal — bounded legacy diagnostics section", () => {
  it("renders the legacy diagnostics note + process tree when legacy data is present", async () => {
    const details = { ...fullDetails, processes: [sampleProcess] };
    vi.stubGlobal(
      "fetch",
      routedFetchMock({ ok: true, data: v2Detail }, { ok: true, data: details }),
    );
    const { container } = render(
      <RunDetailsModal open={true} sessionId="s1" runId="r1" onClose={() => {}} />
    );
    await waitFor(() => expect(container.textContent).toContain("run-1"));
    expect(container.textContent).toContain("runDetails.legacyDiagnosticsNote");
    expect(container.textContent).toContain("runDetails.legacyDiagnostics");
    expect(container.querySelector("table")).not.toBeNull();
    expect(container.textContent).toContain("node worker.js");
  });

  it("renders em-dashes for null legacy scalar fields and hides delegation_id", async () => {
    const details = {
      ...fullDetails,
      pid: null,
      target_message_id: null,
      delegation_id: null,
      provider_kind: null,
      startup_phase: null,
      startup_expected_activity: null,
      startup_phase_started_at: null,
      startup_silence_threshold_seconds: null,
      last_activity_at: null,
      last_activity_kind: null,
      stalled_at: null,
      provider: null,
    };
    vi.stubGlobal(
      "fetch",
      routedFetchMock({ ok: true, data: v2Detail }, { ok: true, data: details }),
    );
    const { container } = render(
      <RunDetailsModal open={true} sessionId="s1" runId="r1" onClose={() => {}} />
    );
    await waitFor(() => expect(container.textContent).toContain("run-1"));
    const text = container.textContent!;
    expect(text).not.toContain("deleg-1");
    expect(container.textContent).not.toContain("/tmp/x.jsonl");
  });

  it("provider popen_alive null -> noPopen key, cancelled yes variant", async () => {
    const details = {
      ...fullDetails,
      provider: {
        ...fullDetails.provider!,
        cancelled: true,
        popen_alive: null,
        popen_pid: null,
        provider_kind: null,
        mode: null,
        session_id: null,
        jsonl_path: null,
        run_dir: null,
      },
    };
    vi.stubGlobal(
      "fetch",
      routedFetchMock({ ok: true, data: v2Detail }, { ok: true, data: details }),
    );
    const { container } = render(
      <RunDetailsModal open={true} sessionId="s1" runId="r1" onClose={() => {}} />
    );
    await waitFor(() => expect(container.textContent).toContain("run-1"));
    expect(container.textContent).toContain("common.yes");
    expect(container.textContent).toContain("runDetails.noPopen");
  });

  it("provider popen_alive false -> subprocessGone key", async () => {
    const details = {
      ...fullDetails,
      provider: { ...fullDetails.provider!, popen_alive: false },
    };
    vi.stubGlobal(
      "fetch",
      routedFetchMock({ ok: true, data: v2Detail }, { ok: true, data: details }),
    );
    const { container } = render(
      <RunDetailsModal open={true} sessionId="s1" runId="r1" onClose={() => {}} />
    );
    await waitFor(() => expect(container.textContent).toContain("run-1"));
    expect(container.textContent).toContain("runDetails.subprocessGone");
  });

  it("empty processes -> noPid message; non-empty -> ProcessTable", async () => {
    const { container: emptyC } = render(
      <RunDetailsModal open={true} sessionId="s1" runId="r1" onClose={() => {}} />
    );
    await waitFor(() => expect(emptyC.textContent).toContain("run-1"));
    expect(emptyC.querySelector("table")).toBeNull();
    expect(emptyC.textContent).toContain("runDetails.noPid");

    const details = { ...fullDetails, processes: [sampleProcess] };
    vi.stubGlobal(
      "fetch",
      routedFetchMock({ ok: true, data: v2Detail }, { ok: true, data: details }),
    );
    const { container: tblC } = render(
      <RunDetailsModal open={true} sessionId="s1" runId="r1" onClose={() => {}} />
    );
    await waitFor(() => expect(tblC.querySelector("table")).not.toBeNull());
    expect(tblC.textContent).toContain("node worker.js");
  });
});

describe("Section / KV", () => {
  it("Section renders with and without a subtitle", () => {
    const { container: a } = render(
      <Section title="T" subtitle="sub">x</Section>
    );
    expect(a.textContent).toContain("sub");
    const { container: b } = render(<Section title="T">x</Section>);
    expect(b.textContent).not.toContain("sub");
  });

  it("KV applies monospace style only when mono=true", () => {
    const { container: monoC } = render(<KV k="kk" v="vv" mono />);
    // container divs: [KV root, k-label, v-value] -> value is index 2
    const monoVal = monoC.querySelectorAll("div")[2]!;
    expect(monoVal.getAttribute("style")).toContain("monospace");
    const { container: sansC } = render(<KV k="kk" v="vv" />);
    const sansVal = sansC.querySelectorAll("div")[2]!;
    expect(sansVal.getAttribute("style")).not.toContain("monospace");
  });
});

describe("ProcessTable — stat color + empty-value matrix", () => {
  /** happy-dom strips `var()` from inline color styles, so the color branches
   *  execute but are not observable via getAttribute. Spy on the `color`
   *  setter to capture the value React computes (before validation), which is
   *  the real branch output. */
  function captureColorSets(render_: () => void): string[] {
    const proto = Object.getPrototypeOf(document.createElement("div").style);
    const desc = Object.getOwnPropertyDescriptor(proto, "color")!;
    const calls: string[] = [];
    Object.defineProperty(proto, "color", {
      configurable: true,
      get() {
        return (desc.get as () => string).call(this);
      },
      set(v: string) {
        calls.push(v);
        (desc.set as (v: string) => void).call(this, v);
      },
    });
    try {
      render_();
    } finally {
      Object.defineProperty(proto, "color", desc);
    }
    return calls;
  }

  const DANGER = "var(--danger, #d44)";
  const WARNING = "var(--warning, #d99)";

  it.each([
    { stat: "R", alive: true, expectColor: null },
    { stat: "Zombie", alive: true, expectColor: DANGER },
    { stat: "Disk", alive: true, expectColor: WARNING },
    { stat: "R", alive: false, expectColor: DANGER },
  ])("stat=$stat alive=$alive -> color $expectColor", ({ stat, alive, expectColor }) => {
    const calls = captureColorSets(() =>
      render(<ProcessTable rows={[{ ...sampleProcess, stat, alive }]} />)
    );
    if (expectColor) expect(calls).toContain(expectColor);
    else expect(calls).not.toContain(DANGER);
  });

  it("renders em-dashes for empty/null fields and formats cpu/rss", () => {
    const rows: ProcessEntry[] = [
      { ...sampleProcess, stat: "", ppid: null, elapsed: "", command: "", rss_kb: 0 },
      { ...sampleProcess, cpu_percent: 12.345, rss_kb: 2048 },
    ];
    const { container } = render(<ProcessTable rows={rows} />);
    expect(container.querySelectorAll("tbody tr").length).toBe(2);

    const cells = container.querySelectorAll("tbody tr")[0]!.querySelectorAll("td");
    expect(cells[1]!.textContent).toContain("—"); // ppid null
    expect(cells[2]!.textContent).toContain("—"); // stat empty
    expect(cells[5]!.textContent).toContain("—"); // elapsed empty
    expect(cells[6]!.textContent).toContain("—"); // command empty
    expect(cells[4]!.textContent).toContain("—"); // rss_kb 0 -> fmtMem "—"

    const row2 = container.querySelectorAll("tbody tr")[1]!.querySelectorAll("td");
    expect(row2[3]!.textContent).toContain("12.3"); // cpu_percent toFixed(1)
    expect(row2[4]!.textContent).toContain("2.0 MB"); // fmtMem(2048)
  });

  it("header Th + body Td honor left/right alignment", () => {
    const { container } = render(<ProcessTable rows={[sampleProcess]} />);
    const ths = container.querySelectorAll("thead th");
    expect(ths[0]!.getAttribute("style")).toContain("left"); // PID
    expect(ths[3]!.getAttribute("style")).toContain("right"); // CPU%
    const tds = container.querySelectorAll("tbody tr")[0]!.querySelectorAll("td");
    expect(tds[0]!.getAttribute("style")).toContain("left");
    expect(tds[3]!.getAttribute("style")).toContain("right");
    // command cell carries its title + truncation style
    expect(tds[6]!.getAttribute("title")).toBe("node worker.js");
    expect(tds[6]!.getAttribute("style")).toContain("ellipsis");
  });
});
