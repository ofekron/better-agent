import { describe, expect, it } from "vitest";
import {
  parseVoiceCommand,
  voiceRecognitionLanguage,
  type VoiceCommandState,
} from "../src/lib/voiceActivation";

const idle: VoiceCommandState = { mode: "idle", buffer: "" };

describe("parseVoiceCommand", () => {
  it("creates a new session from the exact command", () => {
    expect(parseVoiceCommand("new session", idle)).toEqual({
      state: idle,
      actions: [{ type: "new-session" }],
    });
  });

  it("ignores unrelated idle transcripts", () => {
    expect(parseVoiceCommand("open the session", idle)).toEqual({
      state: idle,
      actions: [],
    });
  });

  it("opens the prompt input and starts prompt capture", () => {
    expect(parseVoiceCommand("open prompt", idle)).toEqual({
      state: { mode: "prompt", buffer: "" },
      actions: [{ type: "open-prompt" }],
    });
  });

  it("does not write open prompt while already capturing a prompt", () => {
    expect(parseVoiceCommand("open prompt", { mode: "prompt", buffer: "existing text" })).toEqual({
      state: { mode: "prompt", buffer: "existing text" },
      actions: [{ type: "open-prompt" }],
    });
  });

  it("captures prompt text until the send command", () => {
    const first = parseVoiceCommand("prompt refactor the tests", idle);
    expect(first).toEqual({
      state: { mode: "prompt", buffer: "refactor the tests" },
      actions: [{ type: "append-draft", text: "refactor the tests" }],
    });

    expect(parseVoiceCommand("and update snapshots that's all, send", first.state)).toEqual({
      state: idle,
      actions: [{ type: "send-prompt", text: "refactor the tests and update snapshots" }],
    });
  });

  it("starts capture when prompt is spoken by itself", () => {
    const first = parseVoiceCommand("prompt", idle);
    expect(first).toEqual({
      state: { mode: "prompt", buffer: "" },
      actions: [],
    });

    expect(parseVoiceCommand("write the implementation that's all, send", first.state)).toEqual({
      state: idle,
      actions: [{ type: "send-prompt", text: "write the implementation" }],
    });
  });

  it("sends a one-shot prompt command", () => {
    expect(parseVoiceCommand("prompt summarize this that's all send", idle)).toEqual({
      state: idle,
      actions: [{ type: "send-prompt", text: "summarize this" }],
    });
  });

  it("asks for confirmation before closing a dictated prompt", () => {
    const first = parseVoiceCommand("prompt refactor the tests", idle);

    expect(parseVoiceCommand("close prompt", first.state)).toEqual({
      state: { mode: "confirm-close", buffer: "refactor the tests" },
      actions: [{ type: "speak", text: "I'm done" }],
    });
  });

  it("sends the dictated prompt after close confirmation yes", () => {
    const confirming: VoiceCommandState = {
      mode: "confirm-close",
      buffer: "refactor the tests",
    };

    expect(parseVoiceCommand("yes", confirming)).toEqual({
      state: idle,
      actions: [{ type: "send-prompt", text: "refactor the tests" }],
    });
  });

  it("writes close prompt after close confirmation no", () => {
    const confirming: VoiceCommandState = {
      mode: "confirm-close",
      buffer: "write the words",
    };

    expect(parseVoiceCommand("no", confirming)).toEqual({
      state: { mode: "prompt", buffer: "write the words close prompt" },
      actions: [{ type: "append-draft", text: "close prompt" }],
    });
  });

  it("accepts common speech-recognition variants", () => {
    expect(parseVoiceCommand("prompts fix the bug that's all sent", idle)).toEqual({
      state: idle,
      actions: [{ type: "send-prompt", text: "fix the bug" }],
    });
    expect(parseVoiceCommand("pumped fix the bug that's all send", idle)).toEqual({
      state: idle,
      actions: [{ type: "send-prompt", text: "fix the bug" }],
    });
    expect(parseVoiceCommand("bumped fix the bug that's all send", idle)).toEqual({
      state: idle,
      actions: [{ type: "send-prompt", text: "fix the bug" }],
    });
  });

  it("uses English recognition for English command words", () => {
    expect(voiceRecognitionLanguage("he")).toBe("en-US");
    expect(voiceRecognitionLanguage("en-GB")).toBe("en-GB");
  });
});
