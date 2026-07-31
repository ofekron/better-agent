import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import {
  TagFilterAutocomplete,
  type TagFilterOption,
} from "../src/components/TagFilterAutocomplete";

function opt(overrides: Partial<TagFilterOption> = {}): TagFilterOption {
  return {
    key: "k1",
    label: "Alpha",
    kind: "manual",
    ...overrides,
  };
}

/** The component renders a SearchInput; focus + drive it through the
 *  real DOM so keyboard/focus/blur branches fire. */
function field(): HTMLInputElement {
  return screen.getByRole("combobox") as HTMLInputElement;
}

describe("TagFilterAutocomplete — rendering", () => {
  it("renders nothing for chips or dropdown when no options and nothing selected", () => {
    const onToggle = vi.fn();
    const { container } = render(
      <TagFilterAutocomplete options={[]} selectedTagIds={[]} onToggle={onToggle} />,
    );
    expect(container.querySelector(".session-tag-filter-selected")).toBeNull();
    expect(container.querySelector(".session-tag-autocomplete-list")).toBeNull();
    expect(field().value).toBe("");
  });

  it("renders a chip per selected option with manual vs requirement classes", () => {
    const options = [
      opt({ key: "m1", label: "ManualTag", kind: "manual" }),
      opt({ key: "p1", label: "ProdReq", kind: "product", title: "kind: product" }),
      opt({ key: "f1", label: "FeatReq", kind: "feature", title: "kind: feature" }),
    ];
    const onToggle = vi.fn();
    render(
      <TagFilterAutocomplete
        options={options}
        selectedTagIds={["m1", "p1", "f1"]}
        onToggle={onToggle}
      />,
    );
    const chips = screen.getAllByRole("button");
    // 3 chip buttons (the combobox is an input, not a button)
    expect(chips).toHaveLength(3);
    expect(chips[0].className).toContain("session-tag-toggle");
    expect(chips[1].className).toContain("session-requirement-tag-product");
    expect(chips[2].className).toContain("session-requirement-tag-feature");
    expect(chips[1].getAttribute("title")).toBe("kind: product");
  });

  it("clicking a chip toggles its tag off", () => {
    const onToggle = vi.fn();
    render(
      <TagFilterAutocomplete
        options={[opt({ key: "m1", label: "M" })]}
        selectedTagIds={["m1"]}
        onToggle={onToggle}
      />,
    );
    fireEvent.click(screen.getByRole("button"));
    expect(onToggle).toHaveBeenCalledWith("m1");
  });
});

describe("TagFilterAutocomplete — filtering & dropdown", () => {
  it("opens the dropdown on focus and shows unselected options, excluding selected ones", () => {
    const options = [
      opt({ key: "a", label: "Alpha" }),
      opt({ key: "b", label: "Beta" }),
    ];
    render(
      <TagFilterAutocomplete options={options} selectedTagIds={["b"]} onToggle={vi.fn()} />,
    );
    fireEvent.focus(field());
    const list = document.querySelector(".session-tag-autocomplete-list");
    expect(list).not.toBeNull();
    // "b" is selected → excluded from suggestions
    expect(list!.textContent).toContain("Alpha");
    expect(list!.textContent).not.toContain("Beta");
  });

  it("narrows suggestions by label (case-insensitive) as the user types", () => {
    const options = [
      opt({ key: "a", label: "Alpha" }),
      opt({ key: "b", label: "Beta" }),
    ];
    const { container } = render(
      <TagFilterAutocomplete options={options} selectedTagIds={[]} onToggle={vi.fn()} />,
    );
    fireEvent.change(field(), { target: { value: "alp" } });
    const list = container.querySelector(".session-tag-autocomplete-list");
    expect(list!.textContent).toContain("Alpha");
    expect(list!.textContent).not.toContain("Beta");
  });

  it("shows the empty-state message when a query matches nothing", () => {
    const { container } = render(
      <TagFilterAutocomplete
        options={[opt({ key: "a", label: "Alpha" })]}
        selectedTagIds={[]}
        onToggle={vi.fn()}
      />,
    );
    fireEvent.change(field(), { target: { value: "zzz" } });
    expect(container.querySelector(".session-tag-autocomplete-empty")).not.toBeNull();
    // no listbox when suggestions empty
    expect(container.querySelector(".session-tag-autocomplete-list")).toBeNull();
  });

  it("renders the requirement kind marker span for non-manual suggestions", () => {
    const { container } = render(
      <TagFilterAutocomplete
        options={[opt({ key: "p", label: "Prod", kind: "product" })]}
        selectedTagIds={[]}
        onToggle={vi.fn()}
      />,
    );
    fireEvent.focus(field());
    expect(container.querySelector(".session-tag-autocomplete-kind-product")).not.toBeNull();
  });

  it("picks a suggestion on mouse down, clearing the query and toggling", () => {
    const onToggle = vi.fn();
    render(
      <TagFilterAutocomplete
        options={[opt({ key: "a", label: "Alpha" })]}
        selectedTagIds={[]}
        onToggle={onToggle}
      />,
    );
    const input = field();
    fireEvent.focus(input);
    const item = screen.getByText("Alpha").closest("button")!;
    // mouseDown, not click — preventDefault keeps focus before blur
    fireEvent.mouseDown(item);
    expect(onToggle).toHaveBeenCalledWith("a");
    expect(input.value).toBe("");
  });

  it("hovering an option highlights it (aria-selected)", () => {
    const { container } = render(
      <TagFilterAutocomplete
        options={[opt({ key: "a", label: "Alpha" })]}
        selectedTagIds={[]}
        onToggle={vi.fn()}
      />,
    );
    fireEvent.focus(field());
    const item = container.querySelector('button[role="option"]')!;
    expect(item.getAttribute("aria-selected")).toBe("false");
    fireEvent.mouseEnter(item);
    expect(item.getAttribute("aria-selected")).toBe("true");
  });

  it("closes the dropdown on blur", () => {
    const { container } = render(
      <TagFilterAutocomplete
        options={[opt({ key: "a", label: "Alpha" })]}
        selectedTagIds={[]}
        onToggle={vi.fn()}
      />,
    );
    const input = field();
    fireEvent.focus(input);
    expect(container.querySelector(".session-tag-autocomplete-list")).not.toBeNull();
    fireEvent.blur(input);
    expect(container.querySelector(".session-tag-autocomplete-list")).toBeNull();
  });
});

describe("TagFilterAutocomplete — keyboard navigation", () => {
  function twoOptions() {
    return [
      opt({ key: "a", label: "Alpha" }),
      opt({ key: "b", label: "Beta" }),
    ];
  }

  it("ArrowDown cycles highlight with wraparound and ArrowUp wraps from top", () => {
    const { container } = render(
      <TagFilterAutocomplete options={twoOptions()} selectedTagIds={[]} onToggle={vi.fn()} />,
    );
    const input = field();
    fireEvent.focus(input);
    const items = () => container.querySelectorAll('button[role="option"]');

    fireEvent.keyDown(input, { key: "ArrowDown" });
    expect(items()[0].getAttribute("aria-selected")).toBe("true");
    fireEvent.keyDown(input, { key: "ArrowDown" });
    expect(items()[1].getAttribute("aria-selected")).toBe("true");
    // wrap around to 0
    fireEvent.keyDown(input, { key: "ArrowDown" });
    expect(items()[0].getAttribute("aria-selected")).toBe("true");
    // ArrowUp from index 0 wraps to last
    fireEvent.keyDown(input, { key: "ArrowUp" });
    expect(items()[1].getAttribute("aria-selected")).toBe("true");
    // ArrowUp decrements normally
    fireEvent.keyDown(input, { key: "ArrowUp" });
    expect(items()[0].getAttribute("aria-selected")).toBe("true");
  });

  it("Enter picks the highlighted option", () => {
    const onToggle = vi.fn();
    render(
      <TagFilterAutocomplete options={twoOptions()} selectedTagIds={[]} onToggle={onToggle} />,
    );
    const input = field();
    fireEvent.focus(input);
    fireEvent.keyDown(input, { key: "ArrowDown" });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onToggle).toHaveBeenCalledWith("a");
  });

  it("Enter without a highlight is a no-op", () => {
    const onToggle = vi.fn();
    render(
      <TagFilterAutocomplete options={twoOptions()} selectedTagIds={[]} onToggle={onToggle} />,
    );
    const input = field();
    fireEvent.focus(input);
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onToggle).not.toHaveBeenCalled();
  });

  it("Backspace on an empty field removes the last selected chip", () => {
    const onToggle = vi.fn();
    render(
      <TagFilterAutocomplete
        options={[
          opt({ key: "a", label: "Alpha" }),
          opt({ key: "b", label: "Beta" }),
        ]}
        selectedTagIds={["a", "b"]}
        onToggle={onToggle}
      />,
    );
    const input = field();
    fireEvent.focus(input);
    fireEvent.keyDown(input, { key: "Backspace" });
    expect(onToggle).toHaveBeenCalledWith("b");
  });

  it("Backspace with a non-empty query does not remove a chip", () => {
    const onToggle = vi.fn();
    render(
      <TagFilterAutocomplete
        options={[opt({ key: "a", label: "Alpha" })]}
        selectedTagIds={["a"]}
        onToggle={onToggle}
      />,
    );
    const input = field();
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: "x" } });
    fireEvent.keyDown(input, { key: "Backspace" });
    expect(onToggle).not.toHaveBeenCalled();
  });

  it("Backspace on empty field with no selection is a no-op", () => {
    const onToggle = vi.fn();
    render(
      <TagFilterAutocomplete
        options={[opt({ key: "a", label: "Alpha" })]}
        selectedTagIds={[]}
        onToggle={onToggle}
      />,
    );
    const input = field();
    fireEvent.focus(input);
    fireEvent.keyDown(input, { key: "Backspace" });
    expect(onToggle).not.toHaveBeenCalled();
  });

  it("Escape closes the dropdown and resets highlight", () => {
    const { container } = render(
      <TagFilterAutocomplete options={twoOptions()} selectedTagIds={[]} onToggle={vi.fn()} />,
    );
    const input = field();
    fireEvent.focus(input);
    fireEvent.keyDown(input, { key: "ArrowDown" });
    expect(container.querySelector(".session-tag-autocomplete-list")).not.toBeNull();
    fireEvent.keyDown(input, { key: "Escape" });
    expect(container.querySelector(".session-tag-autocomplete-list")).toBeNull();
    // re-open to confirm highlight was reset (ArrowDown lands on 0 again, not 1)
    fireEvent.focus(input);
    fireEvent.keyDown(input, { key: "ArrowDown" });
    const items = container.querySelectorAll('button[role="option"]');
    expect(items[0].getAttribute("aria-selected")).toBe("true");
  });

  it("ArrowDown/ArrowUp are no-ops when there are no suggestions", () => {
    const { container } = render(
      <TagFilterAutocomplete
        options={[opt({ key: "a", label: "Alpha" })]}
        selectedTagIds={["a"]}
        onToggle={vi.fn()}
      />,
    );
    const input = field();
    fireEvent.focus(input);
    // all options selected → suggestions empty; key nav must not throw / open
    fireEvent.keyDown(input, { key: "ArrowDown" });
    fireEvent.keyDown(input, { key: "ArrowUp" });
    expect(container.querySelector(".session-tag-autocomplete-list")).toBeNull();
  });
});
