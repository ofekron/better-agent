import { describe, expect, it } from "vitest";
import { formatWholeJsonMessage } from "../src/utils/formatWholeJsonMessage";

describe("formatWholeJsonMessage", () => {
  it("pretty-prints a whole-message JSON object as a fenced block", () => {
    const raw = JSON.stringify({ query: "x", count: 9, items: [1, 2] });
    const out = formatWholeJsonMessage(raw);
    expect(out.startsWith("```json\n")).toBe(true);
    expect(out).toContain('"query": "x"');
    expect(out).toContain('"count": 9');
  });

  it("pretty-prints JSONL (one object per line)", () => {
    const raw = [
      JSON.stringify({ a: 1 }),
      JSON.stringify({ b: 2 }),
    ].join("\n");
    const out = formatWholeJsonMessage(raw);
    expect(out.startsWith("```json\n")).toBe(true);
    expect(out).toContain('"a": 1');
    expect(out).toContain('"b": 2');
    expect(out).toContain("\n\n"); // pretty-printed objects separated by blank line
  });

  it("leaves prose messages unchanged", () => {
    const prose = "Here is the result: the build passed.";
    expect(formatWholeJsonMessage(prose)).toBe(prose);
  });

  it("leaves prose-with-inline-JSON unchanged (not whole-message JSON)", () => {
    const mixed = "Result: {\"a\":1} and more text";
    expect(formatWholeJsonMessage(mixed)).toBe(mixed);
  });

  it("does not treat scalar JSON (numbers/null/strings) as objects", () => {
    expect(formatWholeJsonMessage("42")).toBe("42");
    expect(formatWholeJsonMessage("null")).toBe("null");
    expect(formatWholeJsonMessage('"just a string"')).toBe('"just a string"');
  });

  it("leaves empty / oversized text unchanged", () => {
    expect(formatWholeJsonMessage("")).toBe("");
    const huge = '{ "a": 1 }'.repeat(20000);
    expect(formatWholeJsonMessage(huge)).toBe(huge);
  });
});
