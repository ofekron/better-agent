import { afterEach, describe, expect, it, vi } from "vitest";
import {
  horizontalCenterScrollTarget,
  horizontalScrollTarget,
  scrollHorizontalItemToCenter,
  scrollHorizontalItemIntoView,
} from "src/utils/tabScroll";

function rect(left: number, width: number): DOMRect {
  return {
    left,
    right: left + width,
    top: 0,
    bottom: 20,
    width,
    height: 20,
    x: left,
    y: 0,
    toJSON: () => ({}),
  };
}

function element({
  left,
  width,
  scrollLeft = 0,
  clientWidth = width,
  scrollWidth = width,
}: {
  left: number;
  width: number;
  scrollLeft?: number;
  clientWidth?: number;
  scrollWidth?: number;
}): HTMLElement {
  const el = document.createElement("div");
  let currentScrollLeft = scrollLeft;

  el.getBoundingClientRect = () => rect(left, width);
  Object.defineProperty(el, "scrollLeft", {
    get: () => currentScrollLeft,
    set: (value) => {
      currentScrollLeft = Number(value);
    },
    configurable: true,
  });
  Object.defineProperty(el, "clientWidth", {
    value: clientWidth,
    configurable: true,
  });
  Object.defineProperty(el, "scrollWidth", {
    value: scrollWidth,
    configurable: true,
  });
  return el;
}

describe("tab horizontal scrolling", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("scrolls just enough to reveal a selected tab hidden on the right", () => {
    const container = element({
      left: 0,
      width: 300,
      clientWidth: 300,
      scrollWidth: 1000,
    });
    const item = element({ left: 420, width: 120 });

    expect(horizontalScrollTarget(container, item)).toBe(240);
  });

  it("scrolls just enough to reveal a selected tab hidden on the left", () => {
    const container = element({
      left: 0,
      width: 300,
      scrollLeft: 360,
      clientWidth: 300,
      scrollWidth: 1000,
    });
    const item = element({ left: -80, width: 120 });

    expect(horizontalScrollTarget(container, item)).toBe(280);
  });

  it("leaves an already visible selected tab alone", () => {
    const container = element({
      left: 0,
      width: 300,
      scrollLeft: 100,
      clientWidth: 300,
      scrollWidth: 1000,
    });
    const item = element({ left: 40, width: 120 });

    expect(horizontalScrollTarget(container, item)).toBeNull();
  });

  it("anchors an oversized selected tab to its start", () => {
    const container = element({
      left: 0,
      width: 300,
      clientWidth: 300,
      scrollWidth: 1000,
    });
    const item = element({ left: 420, width: 420 });

    expect(horizontalScrollTarget(container, item)).toBe(420);
  });

  it("uses only the container scroll API", () => {
    const container = element({
      left: 0,
      width: 300,
      clientWidth: 300,
      scrollWidth: 1000,
    });
    const item = element({ left: 420, width: 120 });
    const scrollTo = vi.fn();

    vi.spyOn(window, "matchMedia").mockReturnValue({
      matches: true,
    } as MediaQueryList);
    container.scrollTo = scrollTo;
    item.scrollIntoView = vi.fn();

    scrollHorizontalItemIntoView(container, item);

    expect(scrollTo).toHaveBeenCalledWith({ left: 240, behavior: "auto" });
    expect(item.scrollIntoView).not.toHaveBeenCalled();
  });

  it("centers a selected tab when there is room on both sides", () => {
    const container = element({
      left: 0,
      width: 300,
      clientWidth: 300,
      scrollWidth: 1000,
    });
    const item = element({ left: 420, width: 120 });

    expect(horizontalCenterScrollTarget(container, item)).toBe(330);
  });

  it("centers a visible selected tab instead of leaving it alone", () => {
    const container = element({
      left: 0,
      width: 300,
      scrollLeft: 100,
      clientWidth: 300,
      scrollWidth: 1000,
    });
    const item = element({ left: 40, width: 120 });

    expect(horizontalCenterScrollTarget(container, item)).toBe(50);
  });

  it("clamps centered selected tab scrolling to the closest edge", () => {
    const container = element({
      left: 0,
      width: 300,
      scrollLeft: 100,
      clientWidth: 300,
      scrollWidth: 1000,
    });
    const item = element({ left: -80, width: 120 });

    expect(horizontalCenterScrollTarget(container, item)).toBe(0);
  });

  it("scrolls selected tabs to the centered target through the container", () => {
    const container = element({
      left: 0,
      width: 300,
      clientWidth: 300,
      scrollWidth: 1000,
    });
    const item = element({ left: 420, width: 120 });
    const scrollTo = vi.fn();

    vi.spyOn(window, "matchMedia").mockReturnValue({
      matches: true,
    } as MediaQueryList);
    container.scrollTo = scrollTo;
    item.scrollIntoView = vi.fn();

    scrollHorizontalItemToCenter(container, item);

    expect(scrollTo).toHaveBeenCalledWith({ left: 330, behavior: "auto" });
    expect(item.scrollIntoView).not.toHaveBeenCalled();
  });
});
