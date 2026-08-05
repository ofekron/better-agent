import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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
const { captured } = vi.hoisted(() => ({
  captured: { ticks: [] as string[], yvals: [] as string[], tips: [] as string[] },
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
  const XAxis: FC = ({ tickFormatter }) => {
    run(tickFormatter, TICKS, captured.ticks);
    return null;
  };
  const Brush: FC = ({ tickFormatter }) => {
    run(tickFormatter, TICKS, captured.ticks);
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
    ComposedChart: passthrough,
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

describe("AnalyticsPage", () => {
  afterEach(() => {
    vi.resetAllMocks();
    captured.ticks.length = 0;
    captured.yvals.length = 0;
    captured.tips.length = 0;
  });

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
});
