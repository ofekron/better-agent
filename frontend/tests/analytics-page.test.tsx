import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AnalyticsPage } from "../src/components/AnalyticsPage";
import { fetchAnalytics, type AnalyticsReport } from "../src/api";

vi.mock("../src/api", () => ({
  fetchAnalytics: vi.fn(),
}));

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string) => key,
  }),
}));

// recharts' ResponsiveContainer renders with 0 size under happy-dom (no real
// layout), so its children — and the tickFormatter/formatter callbacks bound
// to XAxis/YAxis/Tooltip/Brush — never execute. The pure helpers (fmtPct,
// tickFormatter, tooltipFmt) are only reachable through those callbacks, and
// the Pie Cell maps / HBar body only render as ResponsiveContainer children.
// This stub renders children and invokes each formatter callback with
// representative inputs, capturing the formatted outputs for assertions.
type ComposedHandlers = {
  data: Record<string, unknown>[];
  onMouseDown: ((st: { activeLabel?: string | number }) => void) | undefined;
  onMouseMove: ((st: { activeLabel?: string | number }) => void) | undefined;
  onMouseUp: (() => void) | undefined;
  onMouseLeave: (() => void) | undefined;
};

const { captured } = vi.hoisted(() => ({
  captured: {
    ticks: [] as string[],
    yvals: [] as string[],
    tips: [] as string[],
    // One entry per ComposedChart render — drag/zoom interactions are driven
    // by invoking these captured handlers directly inside act().
    charts: [] as ComposedHandlers[],
    // Latest Brush render's committed window (startIndex/endIndex) — the
    // authoritative readout of the chart's zoom state for clamp assertions.
    brush: null as { start: number; end: number } | null,
    // Latest Brush onChange handler — invoked directly to drive onBrushChange.
    brushChange: null as ((range: { startIndex?: number; endIndex?: number }) => void) | null,
  },
}));

vi.mock("recharts", () => {
  type FC = (props: Record<string, unknown>) => unknown;
  const TICKS = [["2026-07-01 12:00"], ["2026-07-01"], ["jul"], ["short"]];
  const YVALS = [[0], [50], [99.9], [1234], [2_500_000]];
  const TIPS = [[1500, "n"], [0, "z"]];
  const run = (fn: unknown, samples: unknown[][], bucket: string[]) => {
    if (typeof fn !== "function") return;
    for (const args of samples) {
      const res = (fn as (...a: unknown[]) => unknown)(...args);
      if (typeof res === "string") bucket.push(res);
      else if (Array.isArray(res)) bucket.push(String(res[0]));
    }
  };
  const passthrough: FC = ({ children }) => children ?? null;
  // ComposedChart renders its children (so XAxis/YAxis/Tooltip formatters
  // still run) AND records its mouse handlers + data so tests can drive the
  // drag-select / zoom interactions by invoking the latest matching entry.
  const ComposedChart: FC = (props) => {
    captured.charts.push({
      data: (props.data as Record<string, unknown>[]) ?? [],
      onMouseDown: props.onMouseDown as ComposedHandlers["onMouseDown"],
      onMouseMove: props.onMouseMove as ComposedHandlers["onMouseMove"],
      onMouseUp: props.onMouseUp as ComposedHandlers["onMouseUp"],
      onMouseLeave: props.onMouseLeave as ComposedHandlers["onMouseLeave"],
    });
    return props.children ?? null;
  };
  const XAxis: FC = ({ tickFormatter }) => {
    run(tickFormatter, TICKS, captured.ticks);
    return null;
  };
  const Brush: FC = ({ tickFormatter, startIndex, endIndex, onChange }) => {
    run(tickFormatter, TICKS, captured.ticks);
    if (typeof startIndex === "number" && typeof endIndex === "number") {
      captured.brush = { start: startIndex, end: endIndex };
    }
    if (typeof onChange === "function") captured.brushChange = onChange as typeof captured.brushChange;
    return null;
  };
  const YAxis: FC = ({ tickFormatter }) => {
    run(tickFormatter, YVALS, captured.yvals);
    return null;
  };
  const Tooltip: FC = ({ formatter }) => {
    run(formatter, TIPS, captured.tips);
    return null;
  };
  return {
    ResponsiveContainer: passthrough,
    PieChart: passthrough,
    ComposedChart,
    BarChart: passthrough,
    Pie: passthrough,
    XAxis,
    YAxis,
    Brush,
    Tooltip,
    Cell: () => null,
    Bar: () => null,
    Line: () => null,
    CartesianGrid: () => null,
    Legend: () => null,
    ReferenceArea: () => null,
  };
});

const emptyReport = {
  range: { start: "2000-01-01T00:00:00", end: "2026-07-06T23:59:59", granularity: "month" },
  providers: [],
  sessions: {
    total: 0,
    messages_total: 0,
    series: [],
    by_provider: [],
    by_model: [],
    by_orchestration: [],
  },
  turns: {
    total: 0,
    series: [],
    by_provider: [],
    by_model: [],
    duration_avg_ms: 0,
    duration_p50_ms: 0,
  },
  llm_calls: {
    total: 0,
    token_usage: {
      input_tokens: 0,
      output_tokens: 0,
      cache_creation_input_tokens: 0,
      cache_read_input_tokens: 0,
      total_tokens: 0,
    },
    series: [],
    by_provider: [],
    by_model: [],
    by_source: [],
    by_reason: [],
    recent: [],
  },
};

const ZERO_TOKEN_USAGE = {
  input_tokens: 0,
  output_tokens: 0,
  cache_creation_input_tokens: 0,
  cache_read_input_tokens: 0,
  total_tokens: 0,
};

function makeReport(overrides: Partial<AnalyticsReport> = {}): AnalyticsReport {
  return {
    range: { start: "2000-01-01T00:00:00", end: "2026-07-06T23:59:59", granularity: "month" },
    providers: [],
    sessions: {
      total: 0,
      user_total: 0,
      messages_total: 0,
      series: [],
      by_provider: [],
      by_model: [],
      by_orchestration: [],
    },
    turns: {
      total: 0,
      series: [],
      by_provider: [],
      by_model: [],
      duration_avg_ms: 0,
      duration_p50_ms: 0,
    },
    llm_calls: {
      total: 0,
      token_usage: { ...ZERO_TOKEN_USAGE },
      series: [],
      by_provider: [],
      by_model: [],
      by_source: [],
      by_reason: [],
      recent: [],
    },
    ...overrides,
  };
}

/** A fully-populated report that mounts every chart surface: sessions/turns/
 * llm time-series, the user-ratio line chart (valueFormatter=fmtPct), the
 * by-orchestration and token-breakdown pies, and the by-provider HBars. */
function richReport(granularity: string): AnalyticsReport {
  return makeReport({
    range: { start: "2026-01-01T00:00:00", end: "2026-07-06T23:59:59", granularity },
    sessions: {
      total: 10,
      user_total: 6,
      messages_total: 30,
      series: [
        { t: "2026-07-01", count: 4, user_count: 2 },
        { t: "2026-07-02", count: 6, user_count: 3 },
        { t: "2026-07-03", count: 2, user_count: 1 },
        { t: "2026-07-04", count: 8, user_count: 5 },
      ],
      by_provider: [
        { kind: "claude", name: "Claude", count: 5 },
        { kind: "codex", name: "Codex", count: 3 },
      ],
      by_model: [{ kind: "claude", model: "haiku", count: 4 }],
      by_orchestration: [
        { mode: "manager", count: 4 },
        { mode: "native", count: 2 },
      ],
    },
    turns: {
      total: 20,
      series: [
        { t: "2026-07-01", count: 5, user_count: 2, duration_ms: 100 },
        { t: "2026-07-02", count: 8, user_count: 4, duration_ms: 200 },
        { t: "2026-07-03", count: 7, user_count: 3, duration_ms: 150 },
      ],
      by_provider: [{ kind: "claude", name: "Claude", turns: 12 }],
      by_model: [{ kind: "claude", model: "haiku", turns: 8 }],
      duration_avg_ms: 500,
      duration_p50_ms: 400,
    },
    llm_calls: {
      total: 9,
      token_usage: {
        input_tokens: 1500,
        output_tokens: 800,
        cache_creation_input_tokens: 0,
        cache_read_input_tokens: 200,
        total_tokens: 2500,
      },
      series: [
        { t: "2026-07-01", count: 3, input_tokens: 500, output_tokens: 300, cache_creation_input_tokens: 0, cache_read_input_tokens: 0, total_tokens: 800 },
        { t: "2026-07-02", count: 4, input_tokens: 600, output_tokens: 300, cache_creation_input_tokens: 0, cache_read_input_tokens: 100, total_tokens: 1000 },
        { t: "2026-07-03", count: 2, input_tokens: 400, output_tokens: 200, cache_creation_input_tokens: 0, cache_read_input_tokens: 100, total_tokens: 700 },
      ],
      by_provider: [{ provider_id: "p1", kind: "claude", name: "Claude", calls: 5, total_tokens: 1500 }],
      by_model: [{ kind: "claude", model: "haiku", calls: 4, total_tokens: 1200 }],
      by_source: [{ source: "ui", calls: 6, total_tokens: 1800 }],
      by_reason: [{ reason: "review", calls: 3, total_tokens: 900 }],
      recent: [],
    },
  });
}

/** A report whose ONLY populated time-series is `sessions` with the given `t`
 * labels, so exactly one ComposedChart mounts — keeping captured handler
 * identity unambiguous for drag/zoom interaction tests. */
function sessionsOnlyReport(labels: string[]): AnalyticsReport {
  return makeReport({
    sessions: {
      total: labels.length,
      user_total: 0,
      messages_total: 0,
      series: labels.map((t, i) => ({ t, count: i + 1, user_count: 0 })),
      by_provider: [],
      by_model: [],
      by_orchestration: [],
    },
  });
}

describe("AnalyticsPage", () => {
  afterEach(() => {
    vi.resetAllMocks();
    captured.ticks.length = 0;
    captured.yvals.length = 0;
    captured.tips.length = 0;
    captured.charts.length = 0;
    captured.brush = null;
    captured.brushChange = null;
  });

  /** Latest captured ComposedChart render whose data matches `pred`. Handlers
   * are re-read on each call because re-renders (state changes from a prior
   * interaction) push fresh entries with updated closures. */
  function chartMatching(pred: (data: Record<string, unknown>[]) => boolean): ComposedHandlers {
    for (let i = captured.charts.length - 1; i >= 0; i--) {
      if (pred(captured.charts[i].data)) return captured.charts[i];
    }
    throw new Error("no captured ComposedChart matched the predicate");
  }

  /** Drag-select helper: press at the label for `a`, move to the label for `b`,
   * then release — mirroring a real drag across the chart surface. The chart is
   * re-resolved before each step because ComposedChart's handlers (notably
   * onMouseUp = commitSelection, which closes over `sel`) get fresh identities
   * on every render, and each setSel here triggers a re-render. */
  function dragSelect(pred: (data: Record<string, unknown>[]) => boolean, a: string, b?: string) {
    act(() => chartMatching(pred).onMouseDown?.({ activeLabel: a }));
    if (b !== undefined) act(() => chartMatching(pred).onMouseMove?.({ activeLabel: b }));
    act(() => chartMatching(pred).onMouseUp?.());
  }

  function statValue(label: string): string | null {
    const node = screen.getByText(label);
    return node.closest(".analytics-stat-card")?.querySelector(".analytics-stat-value")?.textContent ?? null;
  }

  it("loads backend-default all-time analytics by default", async () => {
    vi.mocked(fetchAnalytics).mockResolvedValue(emptyReport);

    render(<AnalyticsPage onBack={() => undefined} />);

    await waitFor(() => {
      expect(fetchAnalytics).toHaveBeenCalledWith(
        undefined,
        undefined,
        "auto",
        expect.any(AbortSignal),
      );
    });
  });

  it("labels partial native data instead of leaving charts loading", async () => {
    vi.mocked(fetchAnalytics).mockResolvedValue({
      ...emptyReport,
      native_data: {
        state: "stale",
        refresh_requested: true,
        partial_metrics: ["turns"],
      },
    });

    render(<AnalyticsPage onBack={() => undefined} />);

    expect((await screen.findByRole("status")).textContent).toBe(
      "analytics.nativePartial",
    );
    expect(screen.queryByText("common.loading")).toBeNull();
  });

  it("aborts a superseded analytics request", async () => {
    const signals: AbortSignal[] = [];
    vi.mocked(fetchAnalytics).mockImplementation(
      async (_start, _end, _granularity, signal) => {
        if (signal) signals.push(signal);
        return emptyReport;
      },
    );

    render(<AnalyticsPage onBack={() => undefined} />);
    await waitFor(() => expect(signals).toHaveLength(1));
    fireEvent.click(screen.getByRole("button", { name: "7d" }));
    await waitFor(() => expect(signals).toHaveLength(2));

    expect(signals[0].aborted).toBe(true);
  });

  it("aborts analytics loading when the page unmounts", async () => {
    let signal: AbortSignal | undefined;
    vi.mocked(fetchAnalytics).mockImplementation(
      async (_start, _end, _granularity, requestSignal) => {
        signal = requestSignal;
        return new Promise<never>(() => undefined);
      },
    );

    const view = render(<AnalyticsPage onBack={() => undefined} />);
    await waitFor(() => expect(signal).toBeDefined());
    view.unmount();

    expect(signal?.aborted).toBe(true);
  });

  it("shows zoom controls for populated time-series charts", async () => {
    vi.mocked(fetchAnalytics).mockResolvedValue({
      ...emptyReport,
      sessions: {
        ...emptyReport.sessions,
        series: [
          { t: "2026-07-01", count: 1 },
          { t: "2026-07-02", count: 2 },
          { t: "2026-07-03", count: 3 },
          { t: "2026-07-04", count: 4 },
          { t: "2026-07-05", count: 5 },
        ],
      },
    });

    render(<AnalyticsPage onBack={() => undefined} />);

    const chart = await screen.findByTestId("analytics-time-series-chart");
    fireEvent.wheel(chart, { deltaY: -100 });

    expect(await screen.findByRole("button", { name: "analytics.resetZoom" })).toBeTruthy();
  });

  it("shows user-turn sub-series on the turns chart", async () => {
    vi.mocked(fetchAnalytics).mockResolvedValue({
      ...emptyReport,
      turns: {
        ...emptyReport.turns,
        series: [
          { t: "2026-07-01", count: 5, user_count: 2, duration_ms: 100 },
          { t: "2026-07-02", count: 8, user_count: 3, duration_ms: 200 },
        ],
      },
    });

    render(<AnalyticsPage onBack={() => undefined} />);

    const charts = await screen.findAllByTestId("analytics-time-series-chart");
    expect(charts.some(
      (chart) => chart.getAttribute("data-series-keys") === "user_count,non_user",
    )).toBe(true);
  });

  it("formats large totals with B/M/k suffixes in stat cards", async () => {
    vi.mocked(fetchAnalytics).mockResolvedValue(
      makeReport({
        sessions: {
          total: 2_500_000_000,
          user_total: 2_500_000,
          messages_total: 42,
          series: [],
          by_provider: [],
          by_model: [],
          by_orchestration: [],
        },
        turns: { total: 2_500, series: [], by_provider: [], by_model: [], duration_avg_ms: 0, duration_p50_ms: 0 },
      }),
    );

    render(<AnalyticsPage onBack={() => undefined} />);
    await screen.findByText("analytics.statSessions");

    expect(statValue("analytics.statSessions")).toBe("2.5B");
    expect(statValue("analytics.statUserSessions")).toBe("2.5M");
    expect(statValue("analytics.statTurns")).toBe("2.5k");
    expect(statValue("analytics.statMessages")).toBe("42");
  });

  it.each([
    [65_000, "1.1m"],
    [1_500, "1.5s"],
    [500, "500ms"],
  ])("formats avg turn duration %ims as %s", async (ms, expected) => {
    vi.mocked(fetchAnalytics).mockResolvedValue(
      makeReport({
        turns: { total: 0, series: [], by_provider: [], by_model: [], duration_avg_ms: ms, duration_p50_ms: 0 },
      }),
    );

    render(<AnalyticsPage onBack={() => undefined} />);
    await screen.findByText("analytics.statAvgTurn");
    expect(statValue("analytics.statAvgTurn")).toBe(expected);
  });

  it("renders the LLM call log with status, errors, and formatted timestamps", async () => {
    vi.mocked(fetchAnalytics).mockResolvedValue(
      makeReport({
        turns: { total: 0, series: [], by_provider: [], by_model: [], duration_avg_ms: 500, duration_p50_ms: 0 },
        llm_calls: {
          total: 3,
          token_usage: { ...ZERO_TOKEN_USAGE },
          series: [],
          by_provider: [],
          by_model: [],
          by_source: [],
          by_reason: [],
          recent: [
            {
              id: "a",
              timestamp: "2026-07-01T12:00:00Z",
              source: "ui",
              reason: "summarize",
              provider_id: "p1",
              provider_kind: "claude",
              provider_name: "Acme",
              model: "haiku",
              prompt_preview: "do thing",
              token_usage: { ...ZERO_TOKEN_USAGE, total_tokens: 1500 },
              success: false,
              error: "boom",
            },
            {
              id: "b",
              timestamp: "not-a-date",
              source: "api",
              reason: "review",
              provider_id: "p2",
              provider_kind: "codex",
              provider_name: "",
              model: "gpt-mini",
              prompt_preview: "",
              token_usage: { ...ZERO_TOKEN_USAGE },
              success: true,
            },
            {
              id: "c",
              source: "ui",
              reason: "draft",
              provider_id: "p3",
              provider_kind: "agy",
              provider_name: "",
              model: "gemini",
              prompt_preview: "hello",
              token_usage: { ...ZERO_TOKEN_USAGE },
            },
          ],
        },
      }),
    );

    render(<AnalyticsPage onBack={() => undefined} />);
    await screen.findByText("analytics.failed");

    expect(screen.getByText("analytics.failed")).toBeTruthy();
    expect(screen.getAllByText("analytics.succeeded")).toHaveLength(2);
    expect(screen.getByText("boom")).toBeTruthy();
    expect(screen.getByText("analytics.noPromptPreview")).toBeTruthy();
    expect(screen.getByText("not-a-date")).toBeTruthy();
    expect(screen.getByText("—")).toBeTruthy();
    expect(screen.getByText("codex")).toBeTruthy();
    expect(screen.getByText("agy")).toBeTruthy();
    expect(screen.getByText("1.5k analytics.tokensShort")).toBeTruthy();
    expect(screen.getAllByRole("article")).toHaveLength(3);
  });

  it("labels unavailable native data", async () => {
    vi.mocked(fetchAnalytics).mockResolvedValue(
      makeReport({ native_data: { state: "unavailable", refresh_requested: true, partial_metrics: [] } }),
    );

    render(<AnalyticsPage onBack={() => undefined} />);
    expect((await screen.findByRole("status")).textContent).toBe("analytics.nativeUnavailable");
  });

  it("drives the custom date range through fetchAnalytics", async () => {
    vi.mocked(fetchAnalytics).mockResolvedValue(makeReport());
    const { container } = render(<AnalyticsPage onBack={() => undefined} />);
    await waitFor(() => expect(fetchAnalytics).toHaveBeenCalled());

    fireEvent.click(screen.getByRole("button", { name: "analytics.custom" }));

    const inputs = container.querySelectorAll('input[type="date"]');
    expect(inputs).toHaveLength(2);

    await waitFor(() =>
      expect(fetchAnalytics).toHaveBeenLastCalledWith(
        expect.any(String),
        expect.any(String),
        "auto",
        expect.any(AbortSignal),
      ),
    );

    fireEvent.change(inputs[0], { target: { value: "2026-01-15" } });
    await waitFor(() =>
      expect(fetchAnalytics).toHaveBeenLastCalledWith(
        "2026-01-15",
        expect.any(String),
        "auto",
        expect.any(AbortSignal),
      ),
    );

    fireEvent.change(inputs[1], { target: { value: "2026-02-20" } });
    await waitFor(() =>
      expect(fetchAnalytics).toHaveBeenLastCalledWith(
        "2026-01-15",
        "2026-02-20",
        "auto",
        expect.any(AbortSignal),
      ),
    );
  });

  it("shows the error banner when loading fails", async () => {
    vi.mocked(fetchAnalytics).mockRejectedValue(new Error("network down"));

    render(<AnalyticsPage onBack={() => undefined} />);
    const banner = await screen.findByText("network down");
    expect(banner.className).toContain("analytics-error");
  });

  it("re-fires load with the selected granularity", async () => {
    vi.mocked(fetchAnalytics).mockResolvedValue(makeReport());
    render(<AnalyticsPage onBack={() => undefined} />);
    await waitFor(() => expect(fetchAnalytics).toHaveBeenCalled());

    fireEvent.click(screen.getByRole("button", { name: "analytics.granularityDay" }));
    await waitFor(() =>
      expect(fetchAnalytics).toHaveBeenLastCalledWith(
        undefined,
        undefined,
        "day",
        expect.any(AbortSignal),
      ),
    );
  });

  it.each([
    ["month", "2026-07-01 12:00"],
    ["hour", "12:00"],
    ["day", "07-01 12:00"],
  ] as const)(
    "mounts every chart and tick-formats axis labels for %s granularity",
    async (granularity, expectedTick) => {
      vi.mocked(fetchAnalytics).mockResolvedValue(richReport(granularity));

      render(<AnalyticsPage onBack={() => undefined} />);
      await screen.findByText("analytics.byOrchestration");

      // Every chart surface mounted: pies (Cell maps), HBars, time-series.
      expect(screen.getByText("analytics.byOrchestration")).toBeTruthy();
      expect(screen.getByText("analytics.sessionsByProvider")).toBeTruthy();
      expect(screen.getByText("analytics.llmTokensBreakdown")).toBeTruthy();
      expect(screen.getByText("analytics.userPromptRatioOverTime")).toBeTruthy();

      // tickFormatter branch for this granularity (axisFmt via XAxis + Brush).
      expect(captured.ticks).toContain(expectedTick);

      // fmtPct covers all three of its branches through the user-ratio YAxis:
      // <=0 -> "0%", middle band -> "N.N%", >=99.95 -> "100%".
      expect(captured.yvals).toEqual(expect.arrayContaining(["0%", "50.0%", "100%"]));

      // tooltipFmt runs the value through fmt: fmt(1500) -> "1.5k".
      expect(captured.tips).toContain("1.5k");
    },
  );

  it("resets the zoom window when the reset button is clicked", async () => {
    vi.mocked(fetchAnalytics).mockResolvedValue(sessionsOnlyReport(["d0", "d1", "d2", "d3", "d4"]));
    render(<AnalyticsPage onBack={() => undefined} />);
    const chart = await screen.findByTestId("analytics-time-series-chart");

    fireEvent.wheel(chart, { deltaY: -100 });
    const reset = await screen.findByRole("button", { name: "analytics.resetZoom" });
    expect(captured.brush).toEqual({ start: 1, end: 4 });

    fireEvent.click(reset);

    expect(screen.queryByRole("button", { name: "analytics.resetZoom" })).toBeNull();
    expect(captured.brush).toEqual({ start: 0, end: 4 });
  });

  it("zooms into a drag selection and renders the selection area", async () => {
    vi.mocked(fetchAnalytics).mockResolvedValue(sessionsOnlyReport(["d0", "d1", "d2", "d3"]));
    render(<AnalyticsPage onBack={() => undefined} />);
    await screen.findByTestId("analytics-time-series-chart");

    const chart = chartMatching(() => true);
    dragSelect(() => true, "d0", "d2");

    expect(captured.brush).toEqual({ start: 0, end: 2 });
    expect(screen.getByRole("button", { name: "analytics.resetZoom" })).toBeTruthy();
  });

  it("swaps a reversed drag selection so the window is ascending", async () => {
    vi.mocked(fetchAnalytics).mockResolvedValue(sessionsOnlyReport(["d0", "d1", "d2", "d3"]));
    render(<AnalyticsPage onBack={() => undefined} />);
    await screen.findByTestId("analytics-time-series-chart");

    dragSelect(() => true, "d2", "d0");

    expect(captured.brush).toEqual({ start: 0, end: 2 });
  });

  it("ignores a drag selection where start and end land on the same point", async () => {
    vi.mocked(fetchAnalytics).mockResolvedValue(sessionsOnlyReport(["d0", "d1", "d2", "d3"]));
    render(<AnalyticsPage onBack={() => undefined} />);
    await screen.findByTestId("analytics-time-series-chart");

    dragSelect(() => true, "d1", "d1");

    expect(captured.brush).toEqual({ start: 0, end: 3 });
    expect(screen.queryByRole("button", { name: "analytics.resetZoom" })).toBeNull();
  });

  it("ignores a drag selection whose labels are not in the data", async () => {
    vi.mocked(fetchAnalytics).mockResolvedValue(sessionsOnlyReport(["d0", "d1", "d2", "d3"]));
    render(<AnalyticsPage onBack={() => undefined} />);
    await screen.findByTestId("analytics-time-series-chart");

    dragSelect(() => true, "nope", "nada");

    expect(captured.brush).toEqual({ start: 0, end: 3 });
    expect(screen.queryByRole("button", { name: "analytics.resetZoom" })).toBeNull();
  });

  it("clears an in-progress drag on mouse leave without committing", async () => {
    vi.mocked(fetchAnalytics).mockResolvedValue(sessionsOnlyReport(["d0", "d1", "d2", "d3"]));
    render(<AnalyticsPage onBack={() => undefined} />);
    await screen.findByTestId("analytics-time-series-chart");

    const chart = chartMatching(() => true);
    // mouseMove with no prior mouseDown exercises the no-op (cur === null) arm.
    act(() => chart.onMouseMove?.({ activeLabel: "d1" }));
    act(() => chart.onMouseDown?.({ activeLabel: "d0" }));
    act(() => chart.onMouseMove?.({ activeLabel: "d2" }));
    act(() => chart.onMouseLeave?.());
    act(() => chart.onMouseUp?.());

    expect(captured.brush).toEqual({ start: 0, end: 3 });
    expect(screen.queryByRole("button", { name: "analytics.resetZoom" })).toBeNull();
  });

  it("does not zoom when the series has fewer than three points", async () => {
    vi.mocked(fetchAnalytics).mockResolvedValue(sessionsOnlyReport(["d0", "d1"]));
    render(<AnalyticsPage onBack={() => undefined} />);
    const chart = await screen.findByTestId("analytics-time-series-chart");

    fireEvent.wheel(chart, { deltaY: -100 });

    expect(captured.brush).toBeNull();
    expect(screen.queryByRole("button", { name: "analytics.resetZoom" })).toBeNull();
  });

  it("clamps a zoom-out so the window cannot run past the last point", async () => {
    vi.mocked(fetchAnalytics).mockResolvedValue(sessionsOnlyReport(["d0", "d1", "d2", "d3", "d4", "d5"]));
    render(<AnalyticsPage onBack={() => undefined} />);
    await screen.findByTestId("analytics-time-series-chart");

    // Drag to an end-anchored window {3,5}, then zoom out: the naive next-end
    // would be 6 (> lastIndex 5), so the clamp pins ne=5 and pulls ns back.
    dragSelect(() => true, "d3", "d5");
    fireEvent.wheel(screen.getByTestId("analytics-time-series-chart"), { deltaY: 100 });

    expect(captured.brush).toEqual({ start: 2, end: 5 });
  });

  it("clamps a zoom-out so the window cannot run before the first point", async () => {
    vi.mocked(fetchAnalytics).mockResolvedValue(
      sessionsOnlyReport(["d0", "d1", "d2", "d3", "d4", "d5", "d6", "d7"]),
    );
    render(<AnalyticsPage onBack={() => undefined} />);
    await screen.findByTestId("analytics-time-series-chart");

    // Drag to {0,5}, then zoom out: the naive next-start computes negative,
    // so the clamp pins ns=0 and extends ne to nextSpan.
    dragSelect(() => true, "d0", "d5");
    fireEvent.wheel(screen.getByTestId("analytics-time-series-chart"), { deltaY: 100 });

    expect(captured.brush).toEqual({ start: 0, end: 7 });
  });

  it("commits a window from a brush selection", async () => {
    vi.mocked(fetchAnalytics).mockResolvedValue(sessionsOnlyReport(["d0", "d1", "d2", "d3", "d4"]));
    render(<AnalyticsPage onBack={() => undefined} />);
    await screen.findByTestId("analytics-time-series-chart");

    expect(captured.brushChange).not.toBeNull();
    act(() => captured.brushChange!({ startIndex: 1, endIndex: 3 }));

    expect(captured.brush).toEqual({ start: 1, end: 3 });
    expect(screen.getByRole("button", { name: "analytics.resetZoom" })).toBeTruthy();
  });

  it("resets the zoom window when the underlying data identity changes", async () => {
    vi.mocked(fetchAnalytics).mockResolvedValueOnce(sessionsOnlyReport(["d0", "d1", "d2", "d3", "d4"]));
    render(<AnalyticsPage onBack={() => undefined} />);
    const chart = await screen.findByTestId("analytics-time-series-chart");

    fireEvent.wheel(chart, { deltaY: -100 });
    await screen.findByRole("button", { name: "analytics.resetZoom" });
    expect(captured.brush).toEqual({ start: 1, end: 4 });

    // Changing granularity triggers a reload with a fresh series array; the
    // TimeSeriesChart stays mounted but its `data` prop changes identity, so
    // the render-phase reset restores the full window.
    vi.mocked(fetchAnalytics).mockResolvedValueOnce(sessionsOnlyReport(["d0", "d1", "d2", "d3", "d4"]));
    fireEvent.click(screen.getByRole("button", { name: "analytics.granularityDay" }));

    await waitFor(() => expect(captured.brush).toEqual({ start: 0, end: 4 }));
    expect(screen.queryByRole("button", { name: "analytics.resetZoom" })).toBeNull();
  });
});
