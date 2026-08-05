import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { CompleteEvent } from "../src/components/MessageBubble";

// CompleteEvent is a pure presentational component over the `complete` event's
// data payload. It has three top-level paths:
//   1. success + token_usage -> render an In/Out[/Cache] token summary (or null
//      when every token count is zero).
//   2. success without token_usage -> null.
//   3. failure -> an error block with optional error detail and an optional
//      rate-limited-until line.
// `fmt` (used to format token counts) is unit-covered in messagebubble-helpers;
// this slice locks the component's branch wiring.

const renderComplete = (data: Record<string, unknown>) =>
  render(<CompleteEvent data={data} />).container;

describe("CompleteEvent success path", () => {
  it("renders In/Out token counts when token_usage is present", () => {
    const c = renderComplete({
      success: true,
      token_usage: { input_tokens: 1200, output_tokens: 42 },
    });
    expect(c.querySelector(".event-complete-success")).not.toBeNull();
    // fmt(1200) -> "1.2k", fmt(42) -> "42".
    expect(c.querySelector(".event-complete-success")?.textContent).toContain("In: 1.2k");
    expect(c.querySelector(".event-complete-success")?.textContent).toContain("Out: 42");
    // No cache_read_input_tokens -> no Cache span.
    expect(c.querySelector(".event-complete-success")?.textContent).not.toContain("Cache:");
  });

  it("renders the Cache span only when cache_read_input_tokens > 0", () => {
    const c = renderComplete({
      success: true,
      token_usage: {
        input_tokens: 10,
        output_tokens: 5,
        cache_read_input_tokens: 2_000_000, // fmt -> "2.0M"
      },
    });
    expect(c.querySelector(".event-complete-success")?.textContent).toContain("Cache: 2.0M");
  });

  it("renders nothing when success has token_usage but all token counts are zero", () => {
    // input_tokens + output_tokens === 0 -> the explicit null return.
    const c = renderComplete({
      success: true,
      token_usage: { input_tokens: 0, output_tokens: 0, cache_read_input_tokens: 999 },
    });
    expect(c.firstChild).toBeNull();
  });

  it("renders nothing on success without any token_usage payload", () => {
    const c = renderComplete({ success: true });
    expect(c.firstChild).toBeNull();
  });
});

describe("CompleteEvent failure path", () => {
  it("renders an error block with the error detail when error is present", () => {
    const c = renderComplete({ success: false, error: "boom" });
    expect(c.querySelector(".event-complete-error")).not.toBeNull();
    expect(c.querySelector(".error-title")?.textContent).toBe("Request failed");
    const details = c.querySelectorAll(".error-detail");
    expect(details.length).toBe(1);
    expect(details[0].textContent).toBe("boom");
  });

  it("renders the rate-limited line when rate_limited_until is present", () => {
    const c = renderComplete({
      success: false,
      error: "limited",
      rate_limited_until: "2026-08-05T12:00:00Z",
    });
    const details = c.querySelectorAll(".error-detail");
    expect(details.length).toBe(2);
    // toLocaleTimeString output is locale-dependent; assert on the stable prefix.
    expect(details[1].textContent).toContain("Rate limited until");
  });

  it("renders only the title when neither error nor rate_limited_until is present", () => {
    const c = renderComplete({ success: false });
    expect(c.querySelector(".error-title")?.textContent).toBe("Request failed");
    expect(c.querySelectorAll(".error-detail").length).toBe(0);
  });
});
