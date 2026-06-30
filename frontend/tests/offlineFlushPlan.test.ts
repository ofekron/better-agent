import { describe, expect, it } from "vitest";
import {
  classifyFlushError,
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
    expect(classifyFlushError(new TypeError("Failed to fetch"))).toBe("transient");
    expect(classifyFlushError(new DOMException("aborted", "AbortError"))).toBe("transient");
    expect(classifyFlushError(new HttpStatusError(500, "boom"))).toBe("transient");
    expect(classifyFlushError(new HttpStatusError(503, "down"))).toBe("transient");
    expect(classifyFlushError(new HttpStatusError(429, "slow down"))).toBe("transient");
    expect(classifyFlushError(new HttpStatusError(408, "timeout"))).toBe("transient");
  });

  it("treats merits-based 4xx as permanent (don't block the rest of the backlog)", () => {
    expect(classifyFlushError(new HttpStatusError(400, "bad shape"))).toBe("permanent");
    expect(classifyFlushError(new HttpStatusError(404, "team not ready"))).toBe("permanent");
    expect(classifyFlushError(new HttpStatusError(403, "forbidden"))).toBe("permanent");
  });
});

describe("outcomeForCreateError", () => {
  it("stops the drain on a transient error so action order is preserved", () => {
    const outcome = outcomeForCreateError(new TypeError("offline"), "sess-1");
    expect(outcome.stop).toBe(true);
    expect(outcome.permanentFailureSessionId).toBeUndefined();
  });

  it("does NOT stop the drain on a permanent error, and records the dead session", () => {
    const outcome = outcomeForCreateError(new HttpStatusError(400, "bad"), "sess-1");
    expect(outcome.stop).toBe(false);
    expect(outcome.permanentFailureSessionId).toBe("sess-1");
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
        const err = entry.session.id === "A" ? new HttpStatusError(400, "bad") : null;
        if (err) {
          const outcome = outcomeForCreateError(err, entry.session.id);
          if (outcome.stop) {
            stopped = true;
            break;
          }
          if (outcome.permanentFailureSessionId) failed.add(outcome.permanentFailureSessionId);
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
