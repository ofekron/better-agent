import { describe, expect, it } from "vitest";
import { resolveAskPrompt } from "../src/utils/askPrompt";

describe("resolveAskPrompt — Ask picker prompt source", () => {
  it("falls back to prompt_preview when the user message is an orphan stub", () => {
    // Regression: "Create new session anyway" no-oped because the picker
    // sourced the prompt from the orphan user-message stub (content "")
    // while the real query lived on ask_result.prompt_preview.
    expect(resolveAskPrompt("", "investigate the missing messages")).toBe(
      "investigate the missing messages",
    );
  });

  it("falls back when content is whitespace-only", () => {
    expect(resolveAskPrompt("   ", "real prompt")).toBe("real prompt");
  });

  it("prefers the live user content (verbatim, untrimmed) when present", () => {
    expect(resolveAskPrompt("  my question  ", "preview")).toBe(
      "  my question  ",
    );
  });

  it("returns empty string when both are empty", () => {
    expect(resolveAskPrompt("", undefined)).toBe("");
  });
});
