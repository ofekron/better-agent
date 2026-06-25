import { fireEvent, render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { JsonNode } from "../src/components/JsonNode";

describe("JsonNode tree expansion", () => {
  it("opens the full descendant tree when a JSON node is expanded", () => {
    const { container } = render(<JsonNode value={{ a: { b: { c: 1 } } }} />);

    expect(container.textContent).not.toContain('"c"');

    const rootToggle = container.querySelector(".json-toggle");
    expect(rootToggle).not.toBeNull();
    fireEvent.click(rootToggle!);

    expect(container.textContent).toContain('"a"');
    expect(container.textContent).toContain('"b"');
    expect(container.textContent).toContain('"c"');
  });

  it("collapses all open nodes when the collapse signal changes", () => {
    const value = { a: { b: { c: 1 } } };
    const { container, rerender } = render(
      <JsonNode value={value} collapseSignal={0} />,
    );

    const rootToggle = container.querySelector(".json-toggle");
    expect(rootToggle).not.toBeNull();
    fireEvent.click(rootToggle!);
    expect(container.textContent).toContain('"c"');

    rerender(<JsonNode value={value} collapseSignal={1} />);

    expect(container.textContent).not.toContain('"a"');
    expect(container.textContent).not.toContain('"c"');
  });

  it("does not collapse default-open nodes on first mount", () => {
    const { container } = render(<JsonNode value={{ a: 1 }} defaultOpen />);

    expect(container.textContent).toContain('"a"');
  });
});
