import { describe, expect, it } from "vitest";
import {
  buildTurnSummary,
  classifyOutput,
  cleanOutput,
  containsMarkdownSyntax,
  decodeEscapedUnicodeForDisplay,
  extendedSummary,
  firstLineSummary,
  fmt,
  fmtSize,
  fmtTime,
  hexAlphaToRgba,
  isEffectivelyEmpty,
  isToolResult,
  messageWithHydratedRenderPayload,
  normalizeAssistantContentText,
  parseErrorMessage,
  parseStyleAttrs,
  partitionEventsByParent,
  previewEventsForMessage,
  tryParseJson,
  visibleAssistantOutputTexts,
  visibleEventsRepresentAssistantContent,
  workerPanelComplete,
  workerPanelDefaultOpen,
} from "../src/components/MessageBubble";
import type { ChatMessage, WSEvent, WorkerPanel } from "../src/types";

const ev = (type: string, data: Record<string, unknown>): WSEvent =>
  ({ type, data } as unknown as WSEvent);

const worker = (over: Partial<WorkerPanel> = {}): WorkerPanel =>
  ({
    delegation_id: "d1",
    worker_session_id: "ws1",
    worker_description: "desc",
    is_new: false,
    instructions_preview: "",
    events: [],
    ...over,
  }) as WorkerPanel;

const msg = (over: Partial<ChatMessage> = {}): ChatMessage =>
  ({ ...over } as unknown as ChatMessage);

describe("isEffectivelyEmpty", () => {
  it("treats empty and pure-whitespace strings as empty", () => {
    expect(isEffectivelyEmpty("")).toBe(true);
    expect(isEffectivelyEmpty("   \t\n\r")).toBe(true);
  });

  it("treats zero-width / BOM / soft-hyphen / word-joiner strings as empty", () => {
    expect(isEffectivelyEmpty("\u200B\u200C\u200D")).toBe(true);
    expect(isEffectivelyEmpty("\u2060\uFEFF")).toBe(true);
    expect(isEffectivelyEmpty("\u00AD")).toBe(true);
    expect(isEffectivelyEmpty("\uFEFF\n\u200B\t")).toBe(true);
  });

  it("returns false as soon as any visible character is present", () => {
    expect(isEffectivelyEmpty("\u200Ba\u200B")).toBe(false);
    expect(isEffectivelyEmpty("hi")).toBe(false);
  });
});

describe("decodeEscapedUnicodeForDisplay", () => {
  it("leaves text without escapes unchanged", () => {
    expect(decodeEscapedUnicodeForDisplay("hello world")).toBe("hello world");
  });

  it("decodes valid \\uXXXX escapes into their characters", () => {
    expect(decodeEscapedUnicodeForDisplay("\\u0041\\u0042")).toBe("AB");
    expect(decodeEscapedUnicodeForDisplay("check \\u2705")).toBe("check ✅");
  });

  it("preserves control-char escapes (< U+0020) and invalid hex verbatim", () => {
    expect(decodeEscapedUnicodeForDisplay("\\u0010")).toBe("\\u0010");
    expect(decodeEscapedUnicodeForDisplay("\\u00ZZ")).toBe("\\u00ZZ");
  });

  it("decodes multiple escapes mixed with literal text", () => {
    expect(decodeEscapedUnicodeForDisplay("a\\u0031b\\u0032c")).toBe("a1b2c");
  });
});

describe("firstLineSummary", () => {
  it("returns the first non-empty line, trimmed", () => {
    expect(firstLineSummary("  \n  hello \nworld")).toBe("hello");
  });

  it("returns empty string when there is no non-empty line", () => {
    expect(firstLineSummary("\n \n\t")).toBe("");
  });

  it("truncates lines longer than max to max-1 chars plus an ellipsis", () => {
    const longLine = "x".repeat(120);
    const out = firstLineSummary(longLine);
    expect(out.length).toBe(80);
    expect(out.endsWith("\u2026")).toBe(true);
    expect(out.slice(0, 79)).toBe("x".repeat(79));
  });

  it("honours a custom max", () => {
    expect(firstLineSummary("abcdefgh", 5)).toBe("abcd\u2026");
    expect(firstLineSummary("abc", 5)).toBe("abc");
  });
});

describe("extendedSummary", () => {
  it("returns the trimmed full text", () => {
    expect(extendedSummary("  \nhello\n world \n")).toBe("hello\n world");
  });
  it("returns empty string for whitespace-only input", () => {
    expect(extendedSummary("   \n\t ")).toBe("");
  });
});

describe("containsMarkdownSyntax", () => {
  it("detects headings, bullets, and code fences", () => {
    expect(containsMarkdownSyntax("# Heading")).toBe(true);
    expect(containsMarkdownSyntax("## Sub")).toBe(true);
    expect(containsMarkdownSyntax("- item")).toBe(true);
    expect(containsMarkdownSyntax("* item")).toBe(true);
    expect(containsMarkdownSyntax("+ item")).toBe(true);
    expect(containsMarkdownSyntax("```code```")).toBe(true);
  });

  it("detects bold, inline code, and links", () => {
    expect(containsMarkdownSyntax("**bold**")).toBe(true);
    expect(containsMarkdownSyntax("use `x`")).toBe(true);
    expect(containsMarkdownSyntax("[a](http://b)")).toBe(true);
  });

  it("returns false for plain prose", () => {
    expect(containsMarkdownSyntax("Just a normal sentence.")).toBe(false);
  });
});

describe("parseStyleAttrs", () => {
  it("returns an empty object for an empty / malformed attr string", () => {
    expect(parseStyleAttrs("")).toEqual({});
    expect(parseStyleAttrs("noequalsign")).toEqual({});
  });

  it("parses bold, clamped font-size, background, and clamped alpha", () => {
    expect(parseStyleAttrs("b=1")).toEqual({ fontWeight: "bold" });
    expect(parseStyleAttrs("b=0")).toEqual({});
    expect(parseStyleAttrs("s=2")).toEqual({ fontSize: "2em" });
    expect(parseStyleAttrs("s=5")).toEqual({ fontSize: "3em" }); // clamped to 3
    expect(parseStyleAttrs("s=0")).toEqual({ fontSize: "1em" }); // clamped to 1
    expect(parseStyleAttrs("s=abc")).toEqual({}); // NaN rejected
  });

  it("combines background hex with alpha into rgba, defaulting alpha to 0.2", () => {
    expect(parseStyleAttrs("bg=#ff0000;a=0.5")).toEqual({
      background: "rgba(255, 0, 0, 0.5)",
    });
    expect(parseStyleAttrs("bg=#00ff00")).toEqual({
      background: "rgba(0, 255, 0, 0.2)",
    });
    expect(parseStyleAttrs("bg=#0000ff;a=5")).toEqual({
      background: "rgba(0, 0, 255, 1)",
    }); // alpha clamped to 1
  });

  it("passes through an invalid background hex unchanged via hexAlphaToRgba", () => {
    expect(parseStyleAttrs("bg=#fff;a=0.5")).toEqual({ background: "#fff" });
  });
});

describe("hexAlphaToRgba", () => {
  it("converts a 6-digit hex (with or without #) to rgba", () => {
    expect(hexAlphaToRgba("#ff0000", 0.5)).toBe("rgba(255, 0, 0, 0.5)");
    expect(hexAlphaToRgba("00ff00", 1)).toBe("rgba(0, 255, 0, 1)");
    expect(hexAlphaToRgba("#FFFFFF", 0)).toBe("rgba(255, 255, 255, 0)");
  });
  it("returns the input unchanged when it is not a 6-digit hex", () => {
    expect(hexAlphaToRgba("#fff", 0.5)).toBe("#fff");
    expect(hexAlphaToRgba("red", 0.5)).toBe("red");
  });
});

describe("parseErrorMessage", () => {
  it("extracts the message field from an inline JSON error object", () => {
    expect(parseErrorMessage('{"message":"rate limited"}')).toBe("rate limited");
    expect(parseErrorMessage('pre {"message":"boom"} post')).toBe("boom");
  });

  it("extracts the JSON message after an 'API Error:' marker", () => {
    expect(parseErrorMessage('API Error: {"message":"upstream 500"}')).toBe(
      "upstream 500",
    );
  });

  it("falls back to a 200-char slice of plain text after 'API Error:'", () => {
    const plain = "API Error: something broke badly";
    expect(parseErrorMessage(plain)).toBe("something broke badly");
    const long = "API Error: " + "x".repeat(300);
    expect(parseErrorMessage(long)).toBe("x".repeat(200));
  });

  it("returns null when no error pattern is present", () => {
    expect(parseErrorMessage("just some output")).toBeNull();
  });
});

describe("classifyOutput", () => {
  it("classifies session-start, error, and success emoji prefixes", () => {
    expect(classifyOutput("📋 Session started: abc")).toBe("session");
    expect(classifyOutput("❌ boom")).toBe("error");
    expect(classifyOutput("Failed to authenticate with provider")).toBe("error");
    expect(classifyOutput("API Error: x")).toBe("error");
    expect(classifyOutput("✅ done")).toBe("success");
  });

  it("defaults to text for plain prose", () => {
    expect(classifyOutput("Here is your answer.")).toBe("text");
  });
});

describe("cleanOutput", () => {
  it("strips the speech-bubble prefix and zero-width chars", () => {
    expect(cleanOutput("\u{1F4AC} hello")).toBe("hello");
    expect(cleanOutput("a\u200Bb\u00ADc")).toBe("abc");
  });

  it("decodes escaped unicode sequences", () => {
    expect(cleanOutput("\\u0041\\u0042")).toBe("AB");
  });

  it("returns empty string when only invisible content remains", () => {
    expect(cleanOutput("\u{1F4AC} \u200B\uFEFF")).toBe("");
    expect(cleanOutput("   \u200B  ")).toBe("");
  });

  it("keeps classification-driving emoji (✅ ❌ 📋)", () => {
    expect(cleanOutput("✅ great")).toBe("✅ great");
    expect(cleanOutput("❌ bad")).toBe("❌ bad");
  });
});

describe("tryParseJson", () => {
  it("parses valid object and array JSON", () => {
    expect(tryParseJson('{"a":1}')).toEqual({ a: 1 });
    expect(tryParseJson("[1,2,3]")).toEqual([1, 2, 3]);
  });

  it("parses JSON with leading whitespace", () => {
    expect(tryParseJson('  {"a":1}')).toEqual({ a: 1 });
  });

  it("returns null when the text does not start with { or [", () => {
    expect(tryParseJson("hello")).toBeNull();
    expect(tryParseJson('"string"')).toBeNull();
    expect(tryParseJson("123")).toBeNull();
  });

  it("returns null for malformed JSON that starts with { or [", () => {
    expect(tryParseJson("{not json}")).toBeNull();
    expect(tryParseJson("[1,")).toBeNull();
  });
});

describe("isToolResult", () => {
  it("detects explicit 'Result:' markers, with or without emoji", () => {
    expect(isToolResult("Result: 42")).toBe(true);
    expect(isToolResult("✅ Result: 42")).toBe(true);
  });

  it("detects numbered terminal-style lines", () => {
    expect(isToolResult("1→ first\n2→ second")).toBe(true);
    expect(isToolResult("  1\tdata")).toBe(true);
  });

  it("detects many file paths and ls-style output", () => {
    expect(isToolResult("/a/b/c\n/d/e/f\n/g/h/i\n/j/k/l")).toBe(true);
    expect(isToolResult("total 24\n")).toBe(true);
    expect(isToolResult("drwxr-xr-x  src\n")).toBe(true);
    expect(isToolResult("-rw-r--r--  file.ts\n")).toBe(true);
  });

  it("returns false for plain prose", () => {
    expect(isToolResult("The build passed.")).toBe(false);
  });
});

describe("fmtSize", () => {
  it("formats sub-1000 byte counts as integers", () => {
    expect(fmtSize(0)).toBe("0");
    expect(fmtSize(500)).toBe("500");
    expect(fmtSize(999)).toBe("999");
  });
  it("formats 1000+ as kilobytes with one decimal", () => {
    expect(fmtSize(1000)).toBe("1.0k");
    expect(fmtSize(1500)).toBe("1.5k");
  });
});

describe("fmt", () => {
  it("formats raw counts with k/M suffixes", () => {
    expect(fmt(999)).toBe("999");
    expect(fmt(1000)).toBe("1.0k");
    expect(fmt(1500)).toBe("1.5k");
    expect(fmt(1_000_000)).toBe("1.0M");
    expect(fmt(1_500_000)).toBe("1.5M");
  });
});

describe("fmtTime", () => {
  it("returns null for falsy input", () => {
    expect(fmtTime(undefined)).toBeNull();
    expect(fmtTime("")).toBeNull();
  });

  it("formats a same-day timestamp as HH:MM:SS", () => {
    const now = new Date();
    const iso = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 9, 8, 7).toISOString();
    const out = fmtTime(iso)!;
    expect(out).toMatch(/\d\d:\d\d:\d\d/);
    expect(out).toContain("09:08:07");
  });

  it("formats an older date as MM/DD HH:MM:SS", () => {
    const iso = new Date(2021, 0, 5, 10, 30, 0).toISOString();
    const out = fmtTime(iso)!;
    expect(out).toMatch(/^01\/05 \d{2}:\d{2}:\d{2}$/);
    expect(out).toContain("10:30:00");
  });
});

describe("partitionEventsByParent", () => {
  it("returns empty top-level and empty children for no events", () => {
    const { topLevel, children } = partitionEventsByParent([]);
    expect(topLevel).toEqual([]);
    expect(children.size).toBe(0);
  });

  it("keeps parentless events at top level", () => {
    const a = ev("output", { output: "hi" });
    const { topLevel, children } = partitionEventsByParent([a]);
    expect(topLevel).toEqual([a]);
    expect(children.size).toBe(0);
  });

  it("nests events whose parent_tool_use_id matches a known tool_call", () => {
    const tool = ev("tool_call", { tool_use_id: "t1", name: "Task" });
    const child = ev("output", { parent_tool_use_id: "t1", output: "nested" });
    const { topLevel, children } = partitionEventsByParent([tool, child]);
    expect(topLevel).toEqual([tool]);
    expect(children.get("t1")).toEqual([child]);
  });

  it("drops a stale parent_tool_use_id to top level when the parent is absent", () => {
    const orphan = ev("output", { parent_tool_use_id: "ghost", output: "x" });
    const { topLevel, children } = partitionEventsByParent([orphan]);
    expect(topLevel).toEqual([orphan]);
    expect(children.size).toBe(0);
  });

  it("groups multiple children under the same parent", () => {
    const tool = ev("tool_call", { tool_use_id: "p" });
    const c1 = ev("output", { parent_tool_use_id: "p", output: "1" });
    const c2 = ev("output", { parent_tool_use_id: "p", output: "2" });
    const { topLevel, children } = partitionEventsByParent([tool, c1, c2]);
    expect(topLevel).toEqual([tool]);
    expect(children.get("p")).toEqual([c1, c2]);
  });
});

describe("workerPanelComplete", () => {
  it("is complete when success is explicitly true or false", () => {
    expect(workerPanelComplete(worker({ success: true }))).toBe(true);
    expect(workerPanelComplete(worker({ success: false }))).toBe(true);
  });

  it("is complete when an error string is present", () => {
    expect(workerPanelComplete(worker({ error: "boom" }))).toBe(true);
  });

  it("is not complete when error is null and success is unset", () => {
    expect(workerPanelComplete(worker({ error: null }))).toBe(false);
    expect(workerPanelComplete(worker({}))).toBe(false);
  });
});

describe("workerPanelDefaultOpen", () => {
  const active = new Set(["d1"]);

  it("never auto-opens a creation-kind panel, even when active", () => {
    expect(
      workerPanelDefaultOpen(worker({ delegation_id: "d1", panel_kind: "session_created" }), active),
    ).toBe(false);
    expect(
      workerPanelDefaultOpen(worker({ delegation_id: "d1", panel_kind: "sub_session_created" }), active),
    ).toBe(false);
  });

  it("opens only when the worker is active and not yet complete", () => {
    expect(workerPanelDefaultOpen(worker({ delegation_id: "d1" }), active)).toBe(true);
  });

  it("stays closed when complete even if active", () => {
    expect(workerPanelDefaultOpen(worker({ delegation_id: "d1", success: true }), active)).toBe(false);
  });

  it("stays closed when not in the active set", () => {
    expect(workerPanelDefaultOpen(worker({ delegation_id: "d2" }), active)).toBe(false);
  });
});

describe("normalizeAssistantContentText", () => {
  it("trims each line, drops empties, and collapses CRLF", () => {
    expect(normalizeAssistantContentText("  hello  \r\n  world  ")).toBe("hello\nworld");
  });

  it("strips the speech-bubble prefix via cleanOutput before normalizing", () => {
    expect(normalizeAssistantContentText("\u{1F4AC} hi there")).toBe("hi there");
  });

  it("returns empty string for whitespace-only input", () => {
    expect(normalizeAssistantContentText("  \n\t \r\n")).toBe("");
  });
});

describe("visibleAssistantOutputTexts", () => {
  it("collects top-level plain-text outputs", () => {
    const out = visibleAssistantOutputTexts([
      ev("output", { output: "first answer line" }),
      ev("output", { output: "second answer line" }),
    ]);
    expect(out).toEqual(["first answer line", "second answer line"]);
  });

  it("skips outputs nested under a tool_use and non-text / empty outputs", () => {
    const out = visibleAssistantOutputTexts([
      ev("output", { parent_tool_use_id: "t1", output: "nested result" }),
      ev("output", { output: "❌ boom" }),
      ev("output", { output: "   " }),
      ev("output", { output: "real prose answer" }),
    ]);
    expect(out).toEqual(["real prose answer"]);
  });
});

describe("visibleEventsRepresentAssistantContent", () => {
  it("is true when a single visible output equals the content", () => {
    const events = [ev("output", { output: "the answer is here" })];
    expect(visibleEventsRepresentAssistantContent(events, "the answer is here")).toBe(true);
  });

  it("is true when the joined outputs equal the content", () => {
    const events = [
      ev("output", { output: "part one" }),
      ev("output", { output: "part two" }),
    ];
    expect(visibleEventsRepresentAssistantContent(events, "part one\npart two")).toBe(true);
  });

  it("is false for empty content", () => {
    expect(visibleEventsRepresentAssistantContent([ev("output", { output: "x" })], "")).toBe(false);
  });

  it("is false when content does not match the outputs", () => {
    const events = [ev("output", { output: "something else entirely" })];
    expect(visibleEventsRepresentAssistantContent(events, "totally different content")).toBe(false);
  });
});

describe("buildTurnSummary", () => {
  it("returns 'No output' when there is nothing to summarize and no fallback", () => {
    expect(buildTurnSummary([], 0)).toBe("No output");
  });

  it("returns 'Response' when only a short fallback is present", () => {
    expect(buildTurnSummary([], 0, "short")).toBe("Response");
  });

  it("uses a long fallback (>10 chars) when there are no events", () => {
    expect(buildTurnSummary([], 0, "a meaningful fallback")).toBe("a meaningful fallback");
  });

  it("counts workers and tool calls", () => {
    const tool = ev("tool_call", { tool_use_id: "t1", name: "Bash" });
    expect(buildTurnSummary([tool], 2)).toBe("2 workers — 1 tool call");
    expect(buildTurnSummary([tool], 0)).toBe("1 tool call");
  });

  it("appends the last long-enough text output, skipping short and non-text", () => {
    const events = [
      ev("output", { output: "tiny" }),
      ev("output", { output: "❌ error shown here" }),
      ev("output", { output: "The final real answer text" }),
    ];
    expect(buildTurnSummary(events, 0)).toBe("The final real answer text");
  });
});

describe("messageWithHydratedRenderPayload", () => {
  it("merges events from hydrated and current without losing either", () => {
    const a = ev("output", { output: "from hydrated" });
    const b = ev("output", { output: "from current" });
    const next = messageWithHydratedRenderPayload(
      msg({ events: [b] }),
      msg({ events: [a] }),
    );
    expect(next.events).toHaveLength(2);
    expect(next.events.map((e) => (e.data as { output: string }).output).sort()).toEqual([
      "from current",
      "from hydrated",
    ]);
  });

  it("overrides current worker fields with hydrated ones, merging events", () => {
    const curEvt = ev("output", { output: "current worker event" });
    const hydEvt = ev("output", { output: "hydrated worker event" });
    const next = messageWithHydratedRenderPayload(
      msg({
        workers: [worker({ delegation_id: "d1", events: [curEvt], worker_description: "old" })],
      }),
      msg({
        workers: [worker({ delegation_id: "d1", events: [hydEvt], success: true })],
      }),
    );
    expect(next.workers).toHaveLength(1);
    const w = next.workers![0];
    expect(w.success).toBe(true);
    expect(w.worker_description).toBe("desc");
    expect(w.events).toHaveLength(2);
  });

  it("keeps current-only workers and appends hydrated-only workers", () => {
    const next = messageWithHydratedRenderPayload(
      msg({ workers: [worker({ delegation_id: "d1" })] }),
      msg({ workers: [worker({ delegation_id: "d2" })] }),
    );
    const ids = next.workers!.map((w) => w.delegation_id).sort();
    expect(ids).toEqual(["d1", "d2"]);
  });

  it("leaves workers undefined when neither side has any", () => {
    const next = messageWithHydratedRenderPayload(msg({ events: [] }), msg({ events: [] }));
    expect(next.workers).toBeUndefined();
  });
});

describe("previewEventsForMessage", () => {
  it("returns [] for no message", () => {
    expect(previewEventsForMessage(undefined)).toEqual([]);
  });

  it("returns live events when there is no stub snapshot", () => {
    const live = ev("output", { output: "live text" });
    expect(previewEventsForMessage(msg({ events: [live] }))).toEqual([live]);
  });

  it("returns the stub snapshot when there are no live events", () => {
    const stubEvt = ev("output", { output: "stub text" });
    const out = previewEventsForMessage(
      msg({ events: [], stub: { event_count: 1, last_events: [stubEvt] } }),
    );
    expect(out).toEqual([stubEvt]);
  });

  it("merges stub and live events so a late live append is not lost", () => {
    const stubEvt = ev("output", { output: "stub text" });
    const liveEvt = ev("output", { output: "late live text" });
    const out = previewEventsForMessage(
      msg({ events: [liveEvt], stub: { event_count: 1, last_events: [stubEvt] } }),
    );
    expect(out).toHaveLength(2);
    expect(out.map((e) => (e.data as { output: string }).output).sort()).toEqual([
      "late live text",
      "stub text",
    ]);
  });
});
