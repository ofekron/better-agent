import { describe, expect, it, vi, afterEach } from "vitest";
import { cleanup, render } from "@testing-library/react";

// Unit-level: verify VirtualizedEventList's OWN integration logic (how it
// turns react-virtual's output into DOM) deterministically, independent of
// real browser layout (jsdom/happy-dom have no layout engine, so real
// react-virtual geometry is inherently environment-fragile to assert on —
// see messageBubbleVirtualization.test.tsx for the threshold-gating and
// no-data-loss integration coverage instead).

const { findScrollParentMock } = vi.hoisted(() => ({
  findScrollParentMock: vi.fn(),
}));

vi.mock("../src/utils/scrollParent", () => ({
  findScrollParent: findScrollParentMock,
}));

const { useVirtualizerMock } = vi.hoisted(() => ({ useVirtualizerMock: vi.fn() }));
vi.mock("@tanstack/react-virtual", () => ({
  useVirtualizer: useVirtualizerMock,
}));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

async function importComponent() {
  return import("../src/components/VirtualizedEventList");
}

describe("VirtualizedEventList", () => {
  it("renders only the virtualizer's window, at the right indices, when a scroll owner is found", async () => {
    const scrollOwner = document.createElement("div");
    scrollOwner.getBoundingClientRect = () =>
      ({ top: 0, left: 0, right: 800, bottom: 500, width: 800, height: 500, x: 0, y: 0, toJSON() {} }) as DOMRect;
    document.body.appendChild(scrollOwner);
    findScrollParentMock.mockReturnValue(scrollOwner);
    useVirtualizerMock.mockReturnValue({
      getVirtualItems: () => [
        { key: "k2", index: 2, start: 200 },
        { key: "k3", index: 3, start: 280 },
        { key: "k4", index: 4, start: 360 },
      ],
      getTotalSize: () => 4000,
      measureElement: () => {},
    });

    const { VirtualizedEventList } = await importComponent();
    const items = Array.from({ length: 300 }, (_, i) => <span key={i} data-row={i}>{`row-${i}`}</span>);
    const { container } = render(<VirtualizedEventList items={items} />);

    const root = container.querySelector('[data-testid="virtualized-event-list"]');
    expect(root).not.toBeNull();
    expect((root as HTMLElement).style.height).toBe("4000px");

    const mounted = Array.from(container.querySelectorAll("[data-row]")).map(
      (el) => el.getAttribute("data-row"),
    );
    expect(mounted).toEqual(["2", "3", "4"]);

    const indexEls = Array.from(container.querySelectorAll("[data-index]"));
    expect(indexEls.map((el) => el.getAttribute("data-index"))).toEqual(["2", "3", "4"]);

    document.body.removeChild(scrollOwner);
  });

  it("renders every item (no windowing) when no scroll owner is found yet", async () => {
    findScrollParentMock.mockReturnValue(null);
    useVirtualizerMock.mockReturnValue({
      getVirtualItems: () => [],
      getTotalSize: () => 0,
      measureElement: () => {},
    });

    const { VirtualizedEventList } = await importComponent();
    const items = Array.from({ length: 12 }, (_, i) => <span key={i} data-row={i}>{`row-${i}`}</span>);
    const { container } = render(<VirtualizedEventList items={items} />);

    expect(container.querySelector('[data-testid="virtualized-event-list-fallback"]')).not.toBeNull();
    expect(container.querySelector('[data-testid="virtualized-event-list"]')).toBeNull();
    const mounted = Array.from(container.querySelectorAll("[data-row]")).map(
      (el) => el.getAttribute("data-row"),
    );
    expect(mounted).toEqual(Array.from({ length: 12 }, (_, i) => String(i)));
  });
});
