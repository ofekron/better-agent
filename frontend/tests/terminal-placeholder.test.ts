import { describe, expect, it } from "vitest";
import { finalizeTerminalAssistant } from "../src/hooks/useSession";
import { makeAssistantMsg, makeUserMsg } from "./fixtures";

describe("finalizeTerminalAssistant — phantom 'No output' on failed turns", () => {
  it("drops the trailing streaming placeholder AND stamps the failure on the user message", () => {
    const user = makeUserMsg({ id: "u1", content: "do thing", seq: 0 });
    const placeholder = makeAssistantMsg({
      id: "live-1",
      isStreaming: true,
      content: "",
      events: [],
    });
    // The error path sends NO messages_delta; the backend marks the USER
    // message errored and removes the assistant. Mirror both so the
    // failure stays visible and no empty "No output" turn lingers.
    const next = finalizeTerminalAssistant([user, placeholder], {
      errorText: "kaboom",
    });
    expect(next).toEqual([{ ...user, status: "error", errorText: "kaboom" }]);
  });

  it("stamps an empty errorText onto the user message (presence, not truthiness)", () => {
    const user = makeUserMsg({ id: "u2", seq: 0 });
    const placeholder = makeAssistantMsg({ id: "live-2", isStreaming: true });
    const next = finalizeTerminalAssistant([user, placeholder], { errorText: "" });
    expect(next).toEqual([{ ...user, status: "error", errorText: "" }]);
  });

  it("does not clobber an existing user-message error with a richer text", () => {
    const user = makeUserMsg({
      id: "u3",
      seq: 0,
      status: "error",
      errorText: "send-level detail",
    });
    const placeholder = makeAssistantMsg({ id: "live-3", isStreaming: true });
    // onPromptSendError already marked the user message; keep its text.
    const next = finalizeTerminalAssistant([user, placeholder], { errorText: "vague" });
    expect(next).toEqual([user]);
  });

  it("drops the placeholder even when there is no preceding user message", () => {
    const placeholder = makeAssistantMsg({ id: "live-4", isStreaming: true });
    const next = finalizeTerminalAssistant([placeholder], { errorText: "kaboom" });
    expect(next).toEqual([]);
  });

  it("flips isStreaming off (no removal, no user stamp) on a normal turn_complete terminal", () => {
    const user = makeUserMsg({ id: "u4", seq: 0 });
    const placeholder = makeAssistantMsg({ id: "live-5", isStreaming: true });
    const next = finalizeTerminalAssistant([user, placeholder], {});
    expect(next.length).toBe(2);
    expect(next[0]).toBe(user);
    expect(next[1].id).toBe("live-5");
    expect(next[1].isStreaming).toBe(false);
    expect(next[1].isDetached).toBe(false);
  });

  it("stamps stopped_at / interrupted_by_msg_id on a turn_stopped terminal", () => {
    const placeholder = makeAssistantMsg({ id: "live-6", isStreaming: true });
    const next = finalizeTerminalAssistant([placeholder], {
      stoppedAt: "2024-01-01T00:00:00Z",
      interruptedByMsgId: "u-int",
    });
    expect(next.length).toBe(1);
    expect(next[0].isStreaming).toBe(false);
    expect(next[0].stopped_at).toBe("2024-01-01T00:00:00Z");
    expect(next[0].interrupted_by_msg_id).toBe("u-int");
  });

  it("is a no-op (same ref) when there is no trailing streaming assistant", () => {
    const user = makeUserMsg({ id: "u5", seq: 0 });
    const userInput = [user];
    expect(finalizeTerminalAssistant(userInput, { errorText: "boom" })).toBe(userInput);
    expect(finalizeTerminalAssistant([], { errorText: "boom" })).toEqual([]);
    const done = makeAssistantMsg({ id: "a-done", isStreaming: false });
    const doneInput = [done];
    expect(finalizeTerminalAssistant(doneInput, { stoppedAt: "x" })).toBe(doneInput);
  });
});
