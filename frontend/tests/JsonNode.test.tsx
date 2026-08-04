import { describe, it, expect, vi } from "vitest";
import { render, fireEvent } from "@testing-library/react";

import { JsonNode } from "../src/components/JsonNode";

/** Query helpers operating on the rendered container by CSS class. */
function q(container: HTMLElement, sel: string): Element | null {
  return container.querySelector(sel);
}
function qAll(container: HTMLElement, sel: string): Element[] {
  return Array.from(container.querySelectorAll(sel));
}

describe("JsonNode — primitive leaves", () => {
  it("renders null", () => {
    const { container } = render(<JsonNode value={null} keyName="k" />);
    const line = q(container, ".json-line")!;
    expect(line.className).toBe("json-line");
    expect(q(container, ".json-key")!.textContent).toBe('"k": ');
    expect(q(container, ".json-null")!.textContent).toBe("null");
  });

  it("renders null without a key", () => {
    const { container } = render(<JsonNode value={null} />);
    expect(q(container, ".json-key")).toBeNull();
    expect(q(container, ".json-null")!.textContent).toBe("null");
  });

  it("renders booleans true/false", () => {
    const { container: t } = render(<JsonNode value={true} keyName="a" />);
    expect(q(t, ".json-bool")!.textContent).toBe("true");
    const { container: f } = render(<JsonNode value={false} keyName="b" />);
    expect(q(f, ".json-bool")!.textContent).toBe("false");
    expect(q(f, ".json-key")!.textContent).toBe('"b": ');
  });

  it("renders numbers", () => {
    const { container } = render(<JsonNode value={42} />);
    expect(q(container, ".json-num")!.textContent).toBe("42");
    expect(q(container, ".json-key")).toBeNull();
  });
});

describe("JsonNode — strings via JsonString", () => {
  it("renders a short string plainly (no toggle)", () => {
    const { container } = render(<JsonNode value="hello" keyName="k" />);
    expect(q(container, ".json-str")!.textContent).toBe('"hello"');
    expect(q(container, ".json-str-expandable")).toBeNull();
    expect(q(container, ".json-key")!.textContent).toBe('"k": ');
  });

  it("renders a short string without a key", () => {
    const { container } = render(<JsonNode value="hi" />);
    expect(q(container, ".json-key")).toBeNull();
    expect(q(container, ".json-str")!.textContent).toBe('"hi"');
  });

  it("does not treat embedded-json-looking strings under 2 chars as embedded", () => {
    // "{" trims to length 1 -> tryParseEmbeddedJson returns null before regex.
    const { container } = render(<JsonNode value="{" />);
    expect(q(container, ".json-embedded")).toBeNull();
    // plain strings render wrapped in quotes
    expect(q(container, ".json-str")!.textContent).toBe('"' + "{" + '"');
  });

  it("does not treat non-bracket-prefixed strings as embedded", () => {
    const value = "plain text";
    const { container } = render(<JsonNode value={value} />);
    expect(q(container, ".json-embedded")).toBeNull();
    expect(q(container, ".json-str")!.textContent).toBe('"' + value + '"');
  });

  it("treats bracket-prefixed invalid JSON as a plain string (catch branch)", () => {
    const value = "{a";
    const { container } = render(<JsonNode value={value} />);
    expect(q(container, ".json-embedded")).toBeNull();
    expect(q(container, ".json-str")!.textContent).toBe('"' + value + '"');
  });

  it("renders an embedded object string as a nested JsonNode", () => {
    const embedded = '{"a":1}';
    const { container } = render(<JsonNode value={embedded} keyName="k" />);
    expect(q(container, ".json-embedded")).not.toBeNull();
    expect(q(container, ".json-embedded-tag")!.getAttribute("title")).toBe(
      "Embedded JSON string"
    );
    // nested JsonNode rendered open (defaultOpen) showing the inner key.
    expect(q(container, ".json-key")!.textContent).toBe('"k": ');
    expect(qAll(container, ".json-num").map((e) => e.textContent)).toEqual(["1"]);
  });

  it("renders an embedded array string as a nested JsonNode", () => {
    const { container } = render(<JsonNode value="[10, 20]" />);
    expect(q(container, ".json-embedded")).not.toBeNull();
    // array child keys are undefined -> no .json-key inside children
    const nums = qAll(container, ".json-num").map((e) => e.textContent);
    expect(nums).toEqual(["10", "20"]);
  });

  it("collapses a long string with a 120-char preview and expands on click", () => {
    const long = "x".repeat(150);
    const { container } = render(<JsonNode value={long} keyName="k" />);
    const expandable = q(container, ".json-str-expandable")!;
    expect(expandable.getAttribute("title")).toBe("Click to expand");
    // preview is first 120 chars + ellipsis wrapper, wrapped in quotes
    expect(expandable.textContent).toContain("x".repeat(120));
    // "…" is literal JSX text (not inside {}), so it renders as those 6 chars
    expect(q(container, ".json-str-more")!.textContent).toBe("\\u2026");
    // not yet expanded
    expect(q(container, ".json-str-full")).toBeNull();

    fireEvent.click(expandable);
    // expanded view shows the full value in a <pre>
    expect(q(container, ".json-str-full")!.textContent).toBe(long);
    expect(q(container, ".json-arrow")!.textContent).toBe("▼");
  });

  it("uses the first line as the preview for a multiline string", () => {
    const value = "line1\nline2\nline3";
    const { container } = render(<JsonNode value={value} />);
    const expandable = q(container, ".json-str-expandable")!;
    expect(expandable.textContent).toContain("line1");
    expect(expandable.textContent).not.toContain("line2");
  });

  it("collapses an expanded string back on toggle click", () => {
    const long = "y".repeat(200);
    const { container } = render(<JsonNode value={long} />);
    fireEvent.click(q(container, ".json-str-expandable")!);
    expect(q(container, ".json-str-full")).not.toBeNull();
    fireEvent.click(q(container, ".json-toggle")!);
    expect(q(container, ".json-str-full")).toBeNull();
    expect(q(container, ".json-str-expandable")).not.toBeNull();
  });

  it("resets an expanded string when collapseSignal changes", () => {
    const long = "z".repeat(200);
    const { rerender, container } = render(<JsonNode value={long} collapseSignal={0} />);
    fireEvent.click(q(container, ".json-str-expandable")!);
    expect(q(container, ".json-str-full")).not.toBeNull();
    rerender(<JsonNode value={long} collapseSignal={1} />);
    expect(q(container, ".json-str-full")).toBeNull();
    expect(q(container, ".json-str-expandable")).not.toBeNull();
  });

  it("clicking expandable stops propagation", () => {
    const parentClick = vi.fn();
    const long = "x".repeat(200);
    const { container } = render(
      <div onClick={parentClick}>
        <JsonNode value={long} />
      </div>
    );
    fireEvent.click(q(container, ".json-str-expandable")!);
    expect(parentClick).not.toHaveBeenCalled();
  });
});

describe("JsonNode — empty containers", () => {
  it("renders an empty array", () => {
    const { container } = render(<JsonNode value={[]} keyName="k" />);
    const line = q(container, ".json-line")!;
    expect(line.textContent).toContain("[]");
    expect(q(container, ".json-key")!.textContent).toBe('"k": ');
  });

  it("renders an empty object without a key", () => {
    const { container } = render(<JsonNode value={{}} />);
    expect(q(container, ".json-key")).toBeNull();
    expect(q(container, ".json-line")!.textContent).toContain("{}");
  });
});

describe("JsonNode — containers with entries", () => {
  it("renders a closed array showing item count and a right arrow", () => {
    const { container } = render(<JsonNode value={[1, 2, 3]} keyName="arr" />);
    const toggle = q(container, ".json-toggle")!;
    expect(q(container, ".json-arrow")!.textContent).toBe("▶"); // closed
    expect(toggle.textContent).toContain("[");
    expect(q(container, ".json-preview")!.textContent).toBe(" 3 items ]");
    expect(q(container, ".json-children")).toBeNull();
  });

  it("renders a closed object showing key count", () => {
    const { container } = render(<JsonNode value={{ a: 1, b: 2 }} />);
    expect(q(container, ".json-preview")!.textContent).toBe(" 2 keys }");
    expect(q(container, ".json-arrow")!.textContent).toBe("▶");
  });

  it("opens an array: children rendered with undefined keys, close brace shown", () => {
    const { container } = render(<JsonNode value={[1, 2]} />);
    fireEvent.click(q(container, ".json-toggle")!);
    expect(q(container, ".json-arrow")!.textContent).toBe("▼"); // open
    expect(q(container, ".json-preview")).toBeNull();
    const children = q(container, ".json-children")!;
    expect(children).not.toBeNull();
    // array entries render without .json-key
    expect(qAll(children, ".json-key")).toEqual([]);
    expect(qAll(container, ".json-num").map((e) => e.textContent)).toEqual(["1", "2"]);
    expect(q(container, ".json-brace")!.textContent).toBe("]");
  });

  it("opens an object: children rendered with keys", () => {
    const { container } = render(<JsonNode value={{ x: 1 }} />);
    fireEvent.click(q(container, ".json-toggle")!);
    const children = q(container, ".json-children")!;
    expect(q(children, ".json-key")!.textContent).toBe('"x": ');
    expect(q(container, ".json-brace")!.textContent).toBe("}");
  });

  it("toggle open then closed again", () => {
    const { container } = render(<JsonNode value={{ x: 1 }} />);
    const toggle = q(container, ".json-toggle")!;
    fireEvent.click(toggle); // open
    expect(q(container, ".json-children")).not.toBeNull();
    fireEvent.click(toggle); // close
    expect(q(container, ".json-children")).toBeNull();
    expect(q(container, ".json-preview")).not.toBeNull();
  });

  it("opening with expandAllOnOpen expands descendants, closing collapses them", () => {
    const nested = { outer: { inner: 1 } };
    const { container } = render(<JsonNode value={nested} expandAllOnOpen={true} />);
    fireEvent.click(q(container, ".json-toggle")!); // open outer
    // descendant object should also be open (openDescendants seeded true)
    const innerToggle = q(container, ".json-children .json-toggle")!;
    expect(q(container, ".json-children .json-children")).not.toBeNull();
    fireEvent.click(q(container, ".json-toggle")!); // close outer
    expect(q(container, ".json-children")).toBeNull();
    // reopen -> with expandAllOnOpen the descendants re-expand (openDescendants
    // is re-seeded true by toggleOpen on each open)
    fireEvent.click(q(container, ".json-toggle")!);
    expect(q(container, ".json-children")).not.toBeNull();
    expect(q(container, ".json-children .json-children")).not.toBeNull();
  });

  it("opening with expandAllOnOpen=false keeps descendants collapsed", () => {
    const nested = { outer: { inner: 1 } };
    const { container } = render(<JsonNode value={nested} expandAllOnOpen={false} />);
    fireEvent.click(q(container, ".json-toggle")!);
    expect(q(container, ".json-children")).not.toBeNull();
    expect(q(container, ".json-children .json-children")).toBeNull();
  });

  it("renders open by default when defaultOpen is set", () => {
    const { container } = render(<JsonNode value={{ x: 1 }} defaultOpen />);
    expect(q(container, ".json-children")).not.toBeNull();
    expect(q(container, ".json-arrow")!.textContent).toBe("▼");
  });

  it("renders descendants open by default via defaultOpenDescendants", () => {
    const nested = { outer: { inner: 1 } };
    const { container } = render(
      <JsonNode value={nested} defaultOpen defaultOpenDescendants />
    );
    expect(q(container, ".json-children .json-children")).not.toBeNull();
  });

  it("resets open state when collapseSignal changes", () => {
    const { rerender, container } = render(
      <JsonNode value={{ x: 1 }} collapseSignal={0} />
    );
    fireEvent.click(q(container, ".json-toggle")!);
    expect(q(container, ".json-children")).not.toBeNull();
    rerender(<JsonNode value={{ x: 1 }} collapseSignal={1} />);
    expect(q(container, ".json-children")).toBeNull();
    expect(q(container, ".json-arrow")!.textContent).toBe("▶");
  });
});

describe("JsonNode — top-level keyName on containers", () => {
  it("shows the key on an open container toggle", () => {
    const { container } = render(<JsonNode value={[1]} keyName="arr" defaultOpen />);
    const toggle = q(container, ".json-toggle")!;
    expect(toggle.textContent).toContain('"arr": ');
  });

  it("omits the key when absent on a container toggle", () => {
    const { container } = render(<JsonNode value={[1]} defaultOpen />);
    expect(q(container, ".json-toggle")!.textContent).not.toContain('"":');
  });
});
