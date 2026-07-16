import { describe, expect, it } from "vitest";
import {
  classifyFlushError,
  nextOfflineRetryDeadline,
  outcomeForCreateError,
  shouldSkipDependentSend,
} from "../src/utils/offlineFlush";
import { HttpStatusError } from "../src/utils/offlineRequest";
import type {
  OfflineCreateSessionEntry,
  OfflinePromptEntry,
} from "../src/hooks/useOfflineQueue";

// Regression lock for the reconnect-drain head-of-line-blocking fix.
//
// Before the fix the whole flush loop sat in one try/catch: a queued
// `create_session` that threw a PERMANENT 4xx aborted the entire `for` loop,
// and the 5s retry re-hit the same poison entry forever — so one bad create
// permanently stranded every unrelated session/prompt queued behind it. That
// violates AGENTS.md "Offline-first usability": reconnects must not lose work.

const prompt = (sessionId: string, clientId: string): OfflinePromptEntry => ({
  sessionId,
  clientId,
  prompt: clientId,
  model: "sonnet",
  cwd: "/tmp/project",
});

const create = (sessionId: string, clientId: string): OfflineCreateSessionEntry => ({
  type: "create_session",
  clientId,
  prompt: "hi",
  session: {
    id: sessionId,
    name: "n",
    model: "sonnet",
    reasoning_effort: undefined,
    permission: undefined,
    cwd: "/tmp/project",
    orchestration_mode: "native",
    provider_id: "claude",
    browser_harness_enabled: false,
    browser_harness_headless: true,
    node_id: "primary",
    created_at: "t",
    updated_at: "t",
    messages: [],
    capability_contexts: undefined,
    folder_id: null,
  } as OfflineCreateSessionEntry["session"],
});

describe("classifyFlushError", () => {
  it("treats network/abort/timeout/5xx/429 as transient (retry whole backlog)", () => {
    expect(classifyFlushError(new TypeError("Failed to fetch"))).toBe("retryable");
    expect(classifyFlushError(new DOMException("aborted", "AbortError"))).toBe("retryable");
    expect(classifyFlushError(new HttpStatusError(500, "boom"))).toBe("retryable");
    expect(classifyFlushError(new HttpStatusError(503, "down"))).toBe("retryable");
    expect(classifyFlushError(new HttpStatusError(429, "slow down"))).toBe("retryable");
    expect(classifyFlushError(new HttpStatusError(408, "timeout"))).toBe("retryable");
  });

  it("terminalizes only the backend's authoritative gone response", () => {
    expect(classifyFlushError(new HttpStatusError(410, "permanently deleted"))).toBe("terminal");
    expect(classifyFlushError(new HttpStatusError(400, "bad shape"))).toBe("retryable");
    expect(classifyFlushError(new HttpStatusError(404, "team not ready"))).toBe("retryable");
  });
});

describe("outcomeForCreateError", () => {
  it("stops the drain on a transient error so action order is preserved", () => {
    const outcome = outcomeForCreateError(new TypeError("offline"), "sess-1");
    expect(outcome.stop).toBe(true);
    expect(outcome.scheduleRetry).toBe(true);
    expect(outcome.terminalFailureSessionId).toBeUndefined();
  });

  it("does NOT stop the drain on a permanent error, and records the dead session", () => {
    const outcome = outcomeForCreateError(new HttpStatusError(410, "gone"), "sess-1");
    expect(outcome.stop).toBe(false);
    expect(outcome.scheduleRetry).toBe(false);
    expect(outcome.terminalFailureSessionId).toBe("sess-1");
  });
});

describe("offline retry deadline", () => {
  it("backs off beyond fifteen seconds without a periodic wake loop", () => {
    let attempt = 0;
    let deadline = 0;
    const delays: number[] = [];
    for (let index = 0; index < 6; index += 1) {
      const next = nextOfflineRetryDeadline(attempt, 1_000, 0);
      delays.push(next.dueAt - 1_000);
      attempt = next.attempt;
      deadline = next.dueAt;
    }
    expect(delays).toEqual([2_000, 4_000, 8_000, 16_000, 32_000, 60_000]);
    expect(deadline).toBe(61_000);
  });

  it("bounds jitter and caps retry delay", () => {
    expect(nextOfflineRetryDeadline(20, 10, -1)).toEqual({ attempt: 21, dueAt: 60_010 });
    expect(nextOfflineRetryDeadline(20, 10, 2)).toEqual({ attempt: 21, dueAt: 72_010 });
  });
});

describe("shouldSkipDependentSend", () => {
  it("skips a prompt whose target session's create permanently failed this pass", () => {
    const failed = new Set(["dead-session"]);
    expect(shouldSkipDependentSend(prompt("dead-session", "p1"), failed)).toBe(true);
  });

  it("does not skip a prompt for an unrelated, healthy session", () => {
    const failed = new Set(["dead-session"]);
    expect(shouldSkipDependentSend(prompt("live-session", "p2"), failed)).toBe(false);
  });

  it("never skips create_session entries themselves", () => {
    const failed = new Set(["dead-session"]);
    expect(shouldSkipDependentSend(create("dead-session", "c1"), failed)).toBe(false);
  });

  it("does not skip anything when no creates have failed", () => {
    const failed = new Set<string>();
    expect(shouldSkipDependentSend(prompt("any", "p3"), failed)).toBe(false);
  });
});

describe("head-of-line blocking is broken (end-to-end policy walk)", () => {
  // Simulate one drain pass over: [poison create A, prompt->A, create B,
  // prompt->B]. The old behavior aborted at A and never reached B. The new
  // policy must: mark A failed, skip prompt->A, and still let B + prompt->B
  // through.
  it("a permanent create failure does not strand unrelated queued work", () => {
    const queue = [
      create("A", "cA"),
      prompt("A", "pA"),
      create("B", "cB"),
      prompt("B", "pB"),
    ];
    const failed = new Set<string>();
    const dispatched: string[] = [];
    let stopped = false;

    for (const entry of queue) {
      if (shouldSkipDependentSend(entry, failed)) continue;
      if (entry.type === "create_session") {
        // A is poison (permanent), B is healthy.
        const err = entry.session.id === "A" ? new HttpStatusError(410, "gone") : null;
        if (err) {
          const outcome = outcomeForCreateError(err, entry.session.id);
          if (outcome.stop) {
            stopped = true;
            break;
          }
          if (outcome.terminalFailureSessionId) failed.add(outcome.terminalFailureSessionId);
          continue;
        }
        dispatched.push(entry.clientId);
        continue;
      }
      dispatched.push(entry.clientId);
    }

    expect(stopped).toBe(false);
    // A's create failed, A's prompt was skipped (still buffered), but B's
    // create AND B's prompt both flushed.
    expect(dispatched).toEqual(["cB", "pB"]);
    expect(failed.has("A")).toBe(true);
  });

  it("a transient create failure stops the pass before any later action (order preserved)", () => {
    const queue = [create("A", "cA"), prompt("B", "pB")];
    const failed = new Set<string>();
    const dispatched: string[] = [];
    let stopped = false;

    for (const entry of queue) {
      if (shouldSkipDependentSend(entry, failed)) continue;
      if (entry.type === "create_session") {
        const outcome = outcomeForCreateError(new TypeError("offline"), entry.session.id);
        if (outcome.stop) {
          stopped = true;
          break;
        }
      }
      dispatched.push(entry.clientId);
    }

    expect(stopped).toBe(true);
    expect(dispatched).toEqual([]); // nothing dispatched ahead of the paused create
  });
});
