import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { Fragment } from "react";
import { renderSingleEvent } from "../src/components/MessageBubble";
import type { WSEvent } from "../src/types";

// renderSingleEvent is the single dispatcher that routes a WSEvent to its
// renderer (or null). The individual event components are covered by their
// own slices; THIS slice locks the switch routing itself — including the
// `thinking` looksLikeThinking heuristic, the null early-returns, and the
// worker_event/error/diagnostic/default branches.
//
// The test i18n instance (tests/setup.ts) has empty resources, so t(key)
// returns the key verbatim; ModelFallbackEvent asserts on those stable keys.

const dispatch = (event: Partial<WSEvent>) => {
  const el = renderSingleEvent(event as WSEvent, 0);
  return render(<Fragment>{el}</Fragment>).container;
};

describe("renderSingleEvent thinking heuristic", () => {
  it("routes a short, keyword-free thought to OutputEvent", () => {
    // length <= 200 AND no keyword -> looksLikeThinking false -> OutputEvent.
    const c = dispatch({ type: "thinking", data: { thought: "hello world" } });
    expect(c.querySelector(".thinking-block")).toBeNull();
    expect(c.textContent).toContain("hello world");
  });

  it("routes a thought containing a thinking keyword to ThinkingBlock", () => {
    // "let me" matches the keyword regex -> ThinkingBlock.
    const c = dispatch({ type: "thinking", data: { thought: "let me check" } });
    expect(c.querySelector(".thinking-block")).not.toBeNull();
  });

  it("routes a long thought (>200 chars) to ThinkingBlock", () => {
    const long = "x".repeat(201);
    const c = dispatch({ type: "thinking", data: { thought: long } });
    expect(c.querySelector(".thinking-block")).not.toBeNull();
  });
});

describe("renderSingleEvent output routing", () => {
  it("renders an `output` event through OutputEvent", () => {
    const c = dispatch({ type: "output", data: { output: "plain output line" } });
    expect(c.textContent).toContain("plain output line");
  });

  it("renders a `tool_result` event through OutputEvent", () => {
    const c = dispatch({ type: "tool_result", data: { output: "tool result line" } });
    expect(c.textContent).toContain("tool result line");
  });
});

describe("renderSingleEvent null early-returns", () => {
  it("returns null for session_discovered", () => {
    expect(dispatch({ type: "session_discovered", data: {} }).firstChild).toBeNull();
  });

  it("returns null for internal turn/user-message lifecycle events", () => {
    const lifecycle = [
      "turn_start",
      "turn_complete",
      "turn_started",
      "turn_stopped",
      "turn_detached",
      "run_state",
      "messages_delta",
      "command_received",
      "user_message_queued",
      "user_message_sent",
      "user_message_received",
      "user_message_done",
      "user_message_failed",
    ] as const;
    for (const type of lifecycle) {
      expect(dispatch({ type, data: {} }).firstChild).toBeNull();
    }
  });
});

describe("renderSingleEvent composed/event renders", () => {
  it("renders a `complete` event through CompleteEvent", () => {
    const c = dispatch({
      type: "complete",
      data: { success: true, token_usage: { input_tokens: 5, output_tokens: 3 } },
    });
    expect(c.querySelector(".event-complete-success")).not.toBeNull();
  });

  it("renders a `model_fallback` event through ModelFallbackEvent", () => {
    const c = dispatch({
      type: "model_fallback",
      data: { from_model: "alpha", to_model: "beta" },
    });
    expect(c.querySelector(".event-model-switched")).not.toBeNull();
    // Empty i18n -> t(key) returns the key verbatim.
    expect(c.textContent).toContain("message.modelFallback");
  });
});

describe("renderSingleEvent routes to shared event components", () => {
  // These components have their own behavior slices; here we only lock the
  // dispatcher routing so the whole switch is covered.
  it("routes steer_prompt to SteerPromptEvent", () => {
    const c = dispatch({ type: "steer_prompt", data: { prompt: "do the thing" } });
    expect(c.querySelector(".event-steer-prompt")).not.toBeNull();
    expect(c.textContent).toContain("do the thing");
  });

  it("routes model_switched to ModelSwitchedEvent", () => {
    const c = dispatch({
      type: "model_switched",
      data: { model: "gpt5", previous_model: "gpt4", changed: ["model"] },
    });
    expect(c.querySelector(".event-model-switched")).not.toBeNull();
  });

  it("routes lifecycle_notice to LifecycleNotice", () => {
    const c = dispatch({ type: "lifecycle_notice", data: { message: "a lifecycle notice" } });
    expect(c.textContent).toContain("a lifecycle notice");
  });

  it("routes pr_link to PrLinkEvent", () => {
    const c = dispatch({
      type: "pr_link",
      data: { prNumber: 42, prUrl: "https://example.com/pr/42", prRepository: "repo" },
    });
    expect(c.querySelector(".event-pr-link")).not.toBeNull();
  });

  it("routes todos_snapshot to TodosSnapshotEvent", () => {
    const c = dispatch({
      type: "todos_snapshot",
      data: { todos: [{ content: "task one", status: "pending" }] },
    });
    expect(c.querySelector(".todos-snapshot")).not.toBeNull();
  });
});

describe("renderSingleEvent worker_event", () => {
  it("returns null when the inner event is absent", () => {
    expect(dispatch({ type: "worker_event", data: {} }).firstChild).toBeNull();
  });

  it("renders the inner event when present", () => {
    const c = dispatch({
      type: "worker_event",
      data: { event: { type: "output", data: { output: "worker output text" } } },
    });
    expect(c.textContent).toContain("worker output text");
  });
});

describe("renderSingleEvent error / diagnostic / default", () => {
  it("renders an `error` event in an event-error block", () => {
    const c = dispatch({ type: "error", data: { error: "something broke" } });
    expect(c.querySelector(".event-error")).not.toBeNull();
    expect(c.textContent).toContain("something broke");
  });

  it("renders a `diagnostic` event through DiagnosticEvent with its kind", () => {
    const c = dispatch({ type: "diagnostic", data: { kind: "warning", raw: "x" } });
    expect(c.querySelector(".event-diagnostic")).not.toBeNull();
    expect(c.querySelector("code")?.textContent).toBe("warning");
  });

  it("renders an unknown event type through DiagnosticEvent as event.<type>", () => {
    const c = dispatch({ type: "totally_unknown_type" as WSEvent["type"], data: { foo: 1 } });
    expect(c.querySelector(".event-diagnostic")).not.toBeNull();
    expect(c.querySelector("code")?.textContent).toBe("event.totally_unknown_type");
  });
});
