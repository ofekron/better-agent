import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../src/lib/frontendLogger", () => ({ logDurable: vi.fn() }));

describe("render profiler incident attribution", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.useFakeTimers();
    vi.stubEnv("BA_RENDER_PROFILER_IN_TESTS", "1");
    window.history.replaceState(null, "", "/");
  });

  async function modules() {
    const logger = await import("../src/lib/frontendLogger");
    const profiler = await import("../src/lib/renderProfiler");
    vi.mocked(logger.logDurable).mockClear();
    return { logger, profiler };
  }

  it("attributes only render work overlapping the causal long task", async () => {
    const { logger, profiler } = await modules();
    vi.spyOn(performance, "now")
      .mockReturnValueOnce(100)
      .mockReturnValueOnce(190)
      .mockReturnValue(300);
    const finish = profiler.perfSpan("event_strategy", { input_events: 400 });
    finish();
    profiler.perfRecord("unrelated", { duration_ms: 5 });

    window.dispatchEvent(new CustomEvent("better-agent:performance-incident", {
      detail: { start_time: 95, duration_ms: 100 },
    }));

    expect(logger.logDurable).toHaveBeenCalledTimes(1);
    expect(logger.logDurable).toHaveBeenCalledWith("render-perf", "event_strategy", expect.objectContaining({
      samples: 1,
      max_duration_ms: 90,
      input_events: 400,
      phase: "causal",
    }));
  });

  it("rejects sub-threshold incidents and expires the post window", async () => {
    const { logger, profiler } = await modules();
    const now = vi.spyOn(performance, "now").mockReturnValue(1_000);
    window.dispatchEvent(new CustomEvent("better-agent:performance-incident", {
      detail: { start_time: 0, duration_ms: 79 },
    }));
    profiler.perfRecord("chat_projection", { duration_ms: 4 });
    vi.advanceTimersByTime(1_000);
    expect(logger.logDurable).not.toHaveBeenCalled();

    window.dispatchEvent(new CustomEvent("better-agent:performance-incident", {
      detail: { start_time: 1_000, duration_ms: 100 },
    }));
    now.mockReturnValue(11_001);
    profiler.perfRecord("chat_projection", { duration_ms: 4 });
    vi.advanceTimersByTime(1_000);
    expect(logger.logDurable).not.toHaveBeenCalledWith(
      "render-perf", "chat_projection", expect.objectContaining({ phase: "after" }),
    );
  });

  it("coalesces repeated post-incident samples and retains max-sample dimensions", async () => {
    const { logger, profiler } = await modules();
    window.dispatchEvent(new CustomEvent("better-agent:performance-incident", {
      detail: { start_time: 0, duration_ms: 100 },
    }));
    profiler.perfRecord("event_strategy", { duration_ms: 12, input_events: 400 });
    profiler.perfRecord("event_strategy", { duration_ms: 18, input_events: 401 });
    vi.advanceTimersByTime(1_000);

    expect(logger.logDurable).toHaveBeenCalledWith("render-perf", "event_strategy", expect.objectContaining({
      samples: 2,
      total_duration_ms: 30,
      max_duration_ms: 18,
      input_events: 401,
      phase: "after",
    }));
  });

  it("bounds stage cardinality and does not create performance marks automatically", async () => {
    const { logger, profiler } = await modules();
    const mark = vi.spyOn(performance, "mark");
    window.dispatchEvent(new CustomEvent("better-agent:performance-incident", {
      detail: { start_time: 0, duration_ms: 100 },
    }));
    for (let index = 0; index < 100; index += 1) {
      profiler.perfRecord(`stage-${index}`, { duration_ms: index });
    }
    vi.advanceTimersByTime(1_000);
    expect(logger.logDurable.mock.calls.filter((call) => call[2]?.phase === "after")).toHaveLength(32);
    expect(mark).not.toHaveBeenCalled();
  });

  it("marks causal totals truncated when overlapping samples were evicted", async () => {
    const { logger, profiler } = await modules();
    vi.spyOn(performance, "now").mockReturnValue(500);
    for (let index = 0; index < 300; index += 1) {
      profiler.perfRecord("event_strategy", { duration_ms: 400, input_events: index });
    }
    window.dispatchEvent(new CustomEvent("better-agent:performance-incident", {
      detail: { start_time: 100, duration_ms: 450 },
    }));

    expect(logger.logDurable).toHaveBeenCalledWith("render-perf", "event_strategy", expect.objectContaining({
      samples: 256,
      evidence_truncated: true,
      dropped_sample_count: 44,
      evidence_generation: 0,
      phase: "causal",
    }));

    vi.mocked(logger.logDurable).mockClear();
    window.dispatchEvent(new CustomEvent("better-agent:performance-incident", {
      detail: { start_time: 110, duration_ms: 430 },
    }));
    expect(logger.logDurable).toHaveBeenCalledWith("render-perf", "event_strategy", expect.objectContaining({
      samples: 256,
      evidence_truncated: true,
      dropped_sample_count: 44,
      evidence_generation: 0,
      phase: "causal",
    }));
  });

  it("ages eviction evidence only after an incident moves beyond its horizon", async () => {
    const { logger, profiler } = await modules();
    const now = vi.spyOn(performance, "now").mockReturnValue(500);
    for (let index = 0; index < 300; index += 1) {
      profiler.perfRecord("event_strategy", { duration_ms: 400 });
    }
    window.dispatchEvent(new CustomEvent("better-agent:performance-incident", {
      detail: { start_time: 600, duration_ms: 100 },
    }));
    vi.mocked(logger.logDurable).mockClear();
    now.mockReturnValue(650);
    profiler.perfRecord("event_strategy", { duration_ms: 20 });
    window.dispatchEvent(new CustomEvent("better-agent:performance-incident", {
      detail: { start_time: 600, duration_ms: 80 },
    }));
    expect(logger.logDurable).toHaveBeenCalledWith("render-perf", "event_strategy", expect.objectContaining({
      evidence_truncated: false,
      dropped_sample_count: 0,
      evidence_generation: 1,
    }));
  });

  it("keeps explicit profiling immediate", async () => {
    const { logger, profiler } = await modules();
    window.history.replaceState(null, "", "/?ba_perf=1");
    profiler.perfRecord("chat_projection", { duration_ms: 9 });
    expect(logger.logDurable).toHaveBeenCalledWith("render-perf", "chat_projection", expect.objectContaining({
      duration_ms: 9,
    }));
  });

  it("replaces the prior runtime listener when the module reloads", async () => {
    await modules();
    vi.resetModules();
    const second = await modules();
    window.dispatchEvent(new CustomEvent("better-agent:performance-incident", {
      detail: { start_time: 0, duration_ms: 100 },
    }));
    second.profiler.perfRecord("chat_projection", { duration_ms: 9 });
    vi.advanceTimersByTime(1_000);
    expect(second.logger.logDurable).toHaveBeenCalledTimes(1);
  });
});
