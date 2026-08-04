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

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(resp({ ok: true, status: 200, data: fullDetails }))
  );
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

describe("RunDetailsModal — open/close + data fetching", () => {
  it("renders nothing when closed", () => {
    const { container } = render(
      <RunDetailsModal open={false} sessionId="s1" runId="r1" onClose={() => {}} />
    );
    expect(container.firstChild).toBeNull();
    expect(fetchMock()).not.toHaveBeenCalled();
  });

  it("fetches details on open and renders the body with kind in the title", async () => {
    const { container } = render(
      <RunDetailsModal open={true} sessionId="s 1" runId="r/1" onClose={() => {}} />
    );
    // sessionId and runId are URL-encoded into the request path.
    await waitFor(() => {
      expect(container.textContent).toContain("run-1");
    });
    expect(fetchMock()).toHaveBeenCalledTimes(1);
    expect(fetchMock().mock.calls[0][0]).toBe(
      "https://backend.test/api/sessions/s%201/runs/r%2F1/details"
    );
    // kind appears as the title suffix
    expect(container.querySelector("h2")!.textContent).toContain("manager");
  });

  it("shows the loading state before details resolve", async () => {
    let resolveFetch!: (v: unknown) => void;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockReturnValue(new Promise((r) => { resolveFetch = r; }))
    );
    const { container } = render(
      <RunDetailsModal open={true} sessionId="s1" runId="r1" onClose={() => {}} />
    );
    // refresh button disabled while loading; no run_id rendered yet
    const refresh = container.querySelector(".btn-secondary") as HTMLButtonElement;
    expect(refresh.disabled).toBe(true);
    expect(container.textContent).not.toContain("run-1");

    resolveFetch(resp({ ok: true, status: 200, data: fullDetails }));
    await waitFor(() => {
      expect(container.textContent).toContain("run-1");
    });
    // once loaded, refresh is enabled again
    expect(
      (container.querySelector(".btn-secondary") as HTMLButtonElement).disabled
    ).toBe(false);
  });

  it("surfaces a non-ok response error (text body)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(resp({ ok: false, status: 500, body: "boom" }))
    );
    const { container } = render(
      <RunDetailsModal open={true} sessionId="s1" runId="r1" onClose={() => {}} />
    );
    await waitFor(() => {
      expect(container.textContent).toContain("HTTP 500: boom");
    });
  });

  it("falls back to statusText when the error body cannot be read (text reject)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        resp({ ok: false, status: 502, statusText: "Bad Gateway", textReject: true })
      )
    );
    const { container } = render(
      <RunDetailsModal open={true} sessionId="s1" runId="r1" onClose={() => {}} />
    );
    await waitFor(() => {
      expect(container.textContent).toContain("HTTP 502: Bad Gateway");
    });
  });

  it("surfaces a network/fetch rejection error", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("network down")));
    const { container } = render(
      <RunDetailsModal open={true} sessionId="s1" runId="r1" onClose={() => {}} />
    );
    await waitFor(() => {
      expect(container.textContent).toContain("network down");
    });
  });

  it("falls back to the i18n error key when the thrown error has no message", async () => {
    // empty message -> `(e as Error).message || t(...)` takes the right side
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error()));
    const { container } = render(
      <RunDetailsModal open={true} sessionId="s1" runId="r1" onClose={() => {}} />
    );
    await waitFor(() => {
      expect(container.textContent).toContain("runDetails.failedToLoad");
    });
  });

  it("refresh button re-triggers load", async () => {
    const { container } = render(
      <RunDetailsModal open={true} sessionId="s1" runId="r1" onClose={() => {}} />
    );
    await waitFor(() => expect(container.textContent).toContain("run-1"));
    fireEvent.click(container.querySelector(".btn-secondary")!);
    await waitFor(() => expect(fetchMock()).toHaveBeenCalledTimes(2));
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

describe("RunDetailsBody — field null/value matrix", () => {
  it("renders the run section with present values", async () => {
    const { container } = render(
      <RunDetailsModal open={true} sessionId="s1" runId="r1" onClose={() => {}} />
    );
    await waitFor(() => expect(container.textContent).toContain("run-1"));
    const text = container.textContent!;
    expect(text).toContain("1234"); // pid
    expect(text).toContain("30"); // startup_silence_threshold_seconds
    expect(text).toContain("msg-9"); // target_message_id present
    expect(text).toContain("deleg-1"); // delegation_id present
    // provider section present
    expect(text).toContain("555"); // popen_pid
    expect(text).toContain("/tmp/x.jsonl");
  });

  it("renders em-dashes for null scalar fields and hides delegation_id", async () => {
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
      vi.fn().mockResolvedValue(resp({ ok: true, status: 200, data: details }))
    );
    const { container } = render(
      <RunDetailsModal open={true} sessionId="s1" runId="r1" onClose={() => {}} />
    );
    await waitFor(() => expect(container.textContent).toContain("run-1"));
    const text = container.textContent!;
    expect(text).not.toContain("deleg-1");
    // provider section absent
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
      vi.fn().mockResolvedValue(resp({ ok: true, status: 200, data: details }))
    );
    const { container } = render(
      <RunDetailsModal open={true} sessionId="s1" runId="r1" onClose={() => {}} />
    );
    await waitFor(() => expect(container.textContent).toContain("run-1"));
    // cancelled -> common.yes key path rendered somewhere in provider section
    expect(container.textContent).toContain("common.yes");
    // popen_alive null -> runDetails.noPopen key
    expect(container.textContent).toContain("runDetails.noPopen");
  });

  it("provider popen_alive false -> subprocessGone key", async () => {
    const details = {
      ...fullDetails,
      provider: { ...fullDetails.provider!, popen_alive: false },
    };
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(resp({ ok: true, status: 200, data: details }))
    );
    const { container } = render(
      <RunDetailsModal open={true} sessionId="s1" runId="r1" onClose={() => {}} />
    );
    await waitFor(() => expect(container.textContent).toContain("run-1"));
    expect(container.textContent).toContain("runDetails.subprocessGone");
  });

  it("empty processes -> noPid message; non-empty -> ProcessTable", async () => {
    // default fixture has empty processes
    const { container: emptyC } = render(
      <RunDetailsModal open={true} sessionId="s1" runId="r1" onClose={() => {}} />
    );
    await waitFor(() => expect(emptyC.textContent).toContain("run-1"));
    expect(emptyC.querySelector("table")).toBeNull();
    expect(emptyC.textContent).toContain("runDetails.noPid");

    const details = { ...fullDetails, processes: [sampleProcess] };
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(resp({ ok: true, status: 200, data: details }))
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
