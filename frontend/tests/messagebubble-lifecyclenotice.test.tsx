import { describe, expect, it } from "vitest";
import { fireEvent, render } from "@testing-library/react";
import { LifecycleNotice } from "../src/components/MessageBubble";

// LifecycleNotice renders a compaction notice: an italic message line plus an
// optional collapsible "Compacted prompt" built from a replacement_history
// array. Branch coverage targets: the empty-message early return, the
// replacement_history Array.isArray gate, the per-item object guard
// (null / non-object / object), the role/text type guards, the text-truthy
// filter, and the replacementText ternary (collapsible present vs null).

const renderNotice = (data: Record<string, unknown>) =>
  render(<LifecycleNotice data={data} />);

describe("LifecycleNotice empty guard", () => {
  it("renders nothing when message is empty", () => {
    const { container } = renderNotice({ message: "" });
    expect(container.firstChild).toBeNull();
  });

  it("renders nothing when message is absent", () => {
    const { container } = renderNotice({});
    expect(container.firstChild).toBeNull();
  });
});

describe("LifecycleNotice message-only (no collapsible)", () => {
  it("renders the message and no CollapsibleOutput when replacement_history is missing", () => {
    const { container } = renderNotice({ message: "compacted" });
    expect(container.textContent).toContain("compacted");
    expect(container.querySelector(".collapsible-output")).toBeNull();
  });

  it("renders no CollapsibleOutput when replacement_history is not an array", () => {
    const { container } = renderNotice({
      message: "m",
      replacement_history: "nope",
    });
    expect(container.querySelector(".collapsible-output")).toBeNull();
  });

  it("renders no CollapsibleOutput when every row is filtered out", () => {
    // null -> !item guard; "junk" -> non-object guard; {role:9,text:""} ->
    // empty-text filter; {text:5} -> non-string text. All collapse to "".
    const { container } = renderNotice({
      message: "m",
      replacement_history: [null, "junk", { role: 9, text: "" }, { text: 5 }],
    });
    expect(container.textContent).toContain("m");
    expect(container.querySelector(".collapsible-output")).toBeNull();
  });
});

describe("LifecycleNotice compacted prompt", () => {
  it("renders a collapsed Compacted prompt and joins valid rows on expand", () => {
    const data = {
      message: "notice",
      replacement_history: [
        { role: "user", text: "hello" },
        { role: "assistant", text: "hi back" },
      ],
    };
    const { container } = renderNotice(data);
    const collapsible = container.querySelector(".collapsible-output")!;
    expect(collapsible).not.toBeNull();
    // defaultOpen=false -> body closed initially.
    expect(collapsible.querySelector(".collapsible-label")!.textContent).toBe(
      "Compacted prompt",
    );
    expect(collapsible.querySelector(".tool-result-pre")).toBeNull();
    // Open it and assert the joined, separator-delimited replacement text.
    fireEvent.click(collapsible.querySelector(".collapsible-output-header")!);
    expect(collapsible.querySelector(".tool-result-pre")!.textContent).toBe(
      "user:\nhello\n\n---\n\nassistant:\nhi back",
    );
  });

  it("defaults a non-string role to 'message'", () => {
    const data = {
      message: "m",
      replacement_history: [{ role: 42, text: "body" }],
    };
    const { container } = renderNotice(data);
    fireEvent.click(container.querySelector(".collapsible-output-header")!);
    expect(container.querySelector(".tool-result-pre")!.textContent).toBe(
      "message:\nbody",
    );
  });
});
