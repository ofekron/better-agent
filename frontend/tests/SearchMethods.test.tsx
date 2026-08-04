import { describe, it, expect, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { SearchMethods } from "../src/components/SearchMethods";
import type { SearchMethod } from "../src/types";

// i18n is initialized in setup.ts with empty resources + fallbackLng: false,
// so t(key) returns the key string verbatim. Asserting against the key proves
// the right LABEL_KEY mapping is wired to the right chip.
const LABEL: Record<SearchMethod, string> = {
  path: "picker.methodPath",
  name: "picker.methodName",
  symbols: "picker.methodSymbols",
};

function chips() {
  return screen.queryAllByRole("button");
}

function chipLabels() {
  return chips().map((b) => b.textContent ?? "");
}

function clickMethod(m: SearchMethod) {
  fireEvent.click(screen.getByText(LABEL[m]));
}

describe("SearchMethods", () => {
  it("renders only available methods in canonical ORDER", () => {
    render(
      <SearchMethods
        available={["symbols", "path"]}
        value={["path"]}
        onChange={() => {}}
      />,
    );
    // ORDER is ["path","name","symbols"]; "name" is not available -> skipped,
    // and the output order follows ORDER, not the available[] input order.
    expect(chipLabels()).toEqual([LABEL.path, LABEL.symbols]);
  });

  it("marks active chips with the active class", () => {
    render(
      <SearchMethods
        available={["path", "name", "symbols"]}
        value={["name"]}
        onChange={() => {}}
      />,
    );
    expect(screen.getByText(LABEL.name).className).toContain("active");
    expect(screen.getByText(LABEL.path).className).not.toContain("active");
    expect(screen.getByText(LABEL.symbols).className).not.toContain("active");
  });

  it("adds an inactive method, emitting value reordered by ORDER", () => {
    const onChange = vi.fn();
    render(
      <SearchMethods
        available={["path", "name", "symbols"]}
        value={["path"]}
        onChange={onChange}
      />,
    );
    clickMethod("symbols");
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith(["path", "symbols"]);
  });

  it("reorders an out-of-order value when adding", () => {
    // value is ["symbols","path"] (reverse of ORDER); adding "name" must emit
    // the full set in ORDER, proving ORDER.filter — not spread — shapes output.
    const onChange = vi.fn();
    render(
      <SearchMethods
        available={["path", "name", "symbols"]}
        value={["symbols", "path"]}
        onChange={onChange}
      />,
    );
    clickMethod("name");
    expect(onChange).toHaveBeenCalledWith(["path", "name", "symbols"]);
  });

  it("removes an active method when more than one is active", () => {
    const onChange = vi.fn();
    render(
      <SearchMethods
        available={["path", "name", "symbols"]}
        value={["path", "name"]}
        onChange={onChange}
      />,
    );
    clickMethod("name");
    expect(onChange).toHaveBeenCalledWith(["path"]);
  });

  it("preserves ORDER when removing from an out-of-order value", () => {
    const onChange = vi.fn();
    render(
      <SearchMethods
        available={["path", "name", "symbols"]}
        value={["symbols", "path"]}
        onChange={onChange}
      />,
    );
    clickMethod("symbols");
    expect(onChange).toHaveBeenCalledWith(["path"]);
  });

  it("never deselects the last active method (invariant: at least one stays)", () => {
    const onChange = vi.fn();
    render(
      <SearchMethods
        available={["path", "name", "symbols"]}
        value={["path"]}
        onChange={onChange}
      />,
    );
    clickMethod("path");
    // The guard fires before onChange — a no-op, not an empty array.
    expect(onChange).not.toHaveBeenCalled();
  });

  it("allows removing one of two active, then blocks once only one remains", () => {
    // Counter-example to the invariant test above: with two active, clicking
    // either is allowed and removes exactly that one; after the value shrinks
    // to a single method, the guard re-engages and the next click is a no-op.
    const onChange = vi.fn();
    const { rerender } = render(
      <SearchMethods
        available={["path", "name", "symbols"]}
        value={["path", "name"]}
        onChange={onChange}
      />,
    );
    clickMethod("path");
    expect(onChange).toHaveBeenLastCalledWith(["name"]);
    rerender(
      <SearchMethods
        available={["path", "name", "symbols"]}
        value={["name"]}
        onChange={onChange}
      />,
    );
    // Now "name" is the sole active — clicking it is a no-op again.
    clickMethod("name");
    expect(onChange).toHaveBeenCalledTimes(1);
  });

  it("does not render methods absent from available even if present in value", () => {
    render(
      <SearchMethods
        available={["path"]}
        value={["path", "symbols"]}
        onChange={() => {}}
      />,
    );
    expect(chipLabels()).toEqual([LABEL.path]);
    expect(screen.queryByText(LABEL.symbols)).toBeNull();
  });

  it("renders nothing interactive when available is empty", () => {
    const { container } = render(
      <SearchMethods available={[]} value={["path"]} onChange={() => {}} />,
    );
    // The wrapper div is still rendered, but holds no chips.
    expect(chips()).toEqual([]);
    expect(container.querySelector(".picker-methods")).not.toBeNull();
  });
});
