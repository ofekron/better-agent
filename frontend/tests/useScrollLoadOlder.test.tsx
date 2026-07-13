import { describe, it, expect, vi } from "vitest";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { scrollToLatest, useScrollLoadOlder } from "../src/hooks/useScrollLoadOlder";

// happy-dom does no layout, so scrollHeight/scrollTop never reflect
// real geometry. We drive them by hand: scrollHeight is backed by a
// mutable closure var, scrollTop is a normal writable property. That's
// enough to exercise the hook's prepend-detection + position-restore
// logic, which only reads those two numbers.
function installGeometry(el: HTMLElement, height: { value: number }) {
  Object.defineProperty(el, "scrollHeight", {
    configurable: true,
    get: () => height.value,
  });
  // happy-dom's native scrollTop setter clamps to its zero-geometry, so
  // override it with a plain writable property to read back the hook's
  // arithmetic faithfully.
  let top = 0;
  Object.defineProperty(el, "scrollTop", {
    configurable: true,
    get: () => top,
    set: (v: number) => {
      top = v;
    },
  });
}

type Hook = ReturnType<typeof useScrollLoadOlder>;

// `tick` is an inert prop that only forces a re-render — it intentionally
// does NOT feed the hook (the hook no longer takes a dep array). This
// mirrors the real Chat: the DOM grows from THROTTLED render data that can
// land in a later commit than the state change that triggered the load.
function Harness({
  onLoadOlder,
  out,
}: {
  tick: number;
  onLoadOlder: () => Promise<void>;
  out: { hook?: Hook };
}) {
  out.hook = useScrollLoadOlder("test-op", true, onLoadOlder);
  return <div ref={out.hook.scrollRef} data-testid="scroller" />;
}

async function mount(node: React.ReactNode) {
  const container = document.createElement("div");
  document.body.appendChild(container);
  let root: Root | null = null;
  await act(async () => {
    root = createRoot(container);
    root.render(node);
  });
  return {
    container,
    rerender: async (next: React.ReactNode) => {
      await act(async () => root?.render(next));
    },
    unmount: async () => {
      await act(async () => root?.unmount());
      container.remove();
    },
  };
}

describe("useScrollLoadOlder", () => {
  it("scrolls an initial committed snapshot to the latest message", () => {
    const element = { scrollTop: 0, scrollHeight: 900 };
    scrollToLatest(element as HTMLDivElement);
    expect(element.scrollTop).toBe(900);
  });
  it("on prepend: restores scroll position and flags justPrepended", async () => {
    const out: { hook?: Hook } = {};
    const height = { value: 100 };
    // Simulate older messages growing the list by 100px when loaded.
    const onLoadOlder = async () => {
      height.value = 200;
    };

    const m = await mount(<Harness tick={0} onLoadOlder={onLoadOlder} out={out} />);
    const el = m.container.querySelector<HTMLDivElement>('[data-testid="scroller"]')!;
    installGeometry(el, height);

    // User is at the top (scrollTop 0) and triggers load-older.
    await act(async () => {
      await out.hook!.triggerLoadOlder();
    });
    // The prepend render fires the layout effect.
    await m.rerender(<Harness tick={1} onLoadOlder={onLoadOlder} out={out} />);

    // Position preserved: the previously-top content shifted down by the
    // 100px of prepended height — NOT snapped to the bottom.
    expect(el.scrollTop).toBe(100);
    expect(out.hook!.justPrepended.current).toBe(true);

    await m.unmount();
  });

  it("on plain append (no load-older): does not flag justPrepended", async () => {
    const out: { hook?: Hook } = {};
    const height = { value: 100 };
    const onLoadOlder = async () => {};

    const m = await mount(<Harness tick={0} onLoadOlder={onLoadOlder} out={out} />);
    const el = m.container.querySelector<HTMLDivElement>('[data-testid="scroller"]')!;
    installGeometry(el, height);

    // A new message arrives: list grows, but no load-older was triggered.
    height.value = 200;
    await m.rerender(<Harness tick={1} onLoadOlder={onLoadOlder} out={out} />);

    expect(out.hook!.justPrepended.current).toBe(false);
    expect(el.scrollTop).toBe(0); // hook left position untouched

    await m.unmount();
  });

  it("does not auto-page for programmatic, focus, or layout scroll events", async () => {
    const out: { hook?: Hook } = {};
    const height = { value: 100 };
    const onLoadOlder = vi.fn(async () => {});
    const m = await mount(<Harness tick={0} onLoadOlder={onLoadOlder} out={out} />);
    const el = m.container.querySelector<HTMLDivElement>('[data-testid="scroller"]')!;
    installGeometry(el, height);

    el.focus();
    el.scrollTop = 100;
    out.hook!.handleWheel({ deltaY: -1, currentTarget: el } as Parameters<Hook["handleWheel"]>[0]);
    out.hook!.handleScroll();
    out.hook!.handleWheel({ deltaY: 1, currentTarget: el } as Parameters<Hook["handleWheel"]>[0]);
    el.scrollTop = 0;
    out.hook!.handleScroll();
    height.value = 200;
    await m.rerender(<Harness tick={1} onLoadOlder={onLoadOlder} out={out} />);
    out.hook!.handleScroll();

    expect(onLoadOlder).not.toHaveBeenCalled();
    await m.unmount();
  });

  it.each([
    ["upward wheel", (hook: Hook, el: HTMLDivElement) => {
      hook.handleWheel({ deltaY: -1, currentTarget: el } as Parameters<Hook["handleWheel"]>[0]);
    }],
    ["upward touchmove", (hook: Hook, el: HTMLDivElement) => {
      hook.handleTouchStart({ touches: [{ clientY: 10 }], currentTarget: el } as unknown as Parameters<Hook["handleTouchStart"]>[0]);
      hook.handleTouchMove({ touches: [{ clientY: 20 }], currentTarget: el } as unknown as Parameters<Hook["handleTouchMove"]>[0]);
    }],
    ["scrollbar pointer", (hook: Hook, el: HTMLDivElement) => {
      Object.defineProperty(el, "clientWidth", { configurable: true, value: 90 });
      Object.defineProperty(el, "clientHeight", { configurable: true, value: 90 });
      el.getBoundingClientRect = () => ({ left: 0, top: 0, right: 100, bottom: 100, width: 100, height: 100, x: 0, y: 0, toJSON: () => ({}) });
      el.setPointerCapture = vi.fn();
      hook.handlePointerDown({ pointerType: "mouse", pointerId: 7, clientX: 95, clientY: 50, currentTarget: el } as Parameters<Hook["handlePointerDown"]>[0]);
    }],
    ["upward navigation key", (hook: Hook, el: HTMLDivElement) => {
      hook.handleKeyDown({ key: "PageUp", currentTarget: el } as Parameters<Hook["handleKeyDown"]>[0]);
    }],
  ])("auto-pages once after %s intent", async (_name, signalIntent) => {
    const out: { hook?: Hook } = {};
    const height = { value: 100 };
    const onLoadOlder = vi.fn(async () => {});
    const m = await mount(<Harness tick={0} onLoadOlder={onLoadOlder} out={out} />);
    const el = m.container.querySelector<HTMLDivElement>('[data-testid="scroller"]')!;
    installGeometry(el, height);

    await act(async () => signalIntent(out.hook!, el));
    out.hook!.handleScroll();
    out.hook!.handleScroll();

    expect(onLoadOlder).toHaveBeenCalledTimes(1);
    await m.unmount();
  });

  it("keeps an upward gesture authorized until it reaches the top", async () => {
    const out: { hook?: Hook } = {};
    const height = { value: 100 };
    const onLoadOlder = vi.fn(async () => {});
    const m = await mount(<Harness tick={0} onLoadOlder={onLoadOlder} out={out} />);
    const el = m.container.querySelector<HTMLDivElement>('[data-testid="scroller"]')!;
    installGeometry(el, height);

    el.scrollTop = 120;
    out.hook!.handleWheel({ deltaY: -1, currentTarget: el } as Parameters<Hook["handleWheel"]>[0]);
    el.scrollTop = 80;
    out.hook!.handleScroll();
    el.scrollTop = 40;
    await act(async () => out.hook!.handleScroll());
    out.hook!.handleScroll();

    expect(onLoadOlder).toHaveBeenCalledTimes(1);
    await m.unmount();
  });

  it("keeps touch authorization through post-touch inertia until top", async () => {
    const out: { hook?: Hook } = {};
    const height = { value: 100 };
    const onLoadOlder = vi.fn(async () => {});
    const m = await mount(<Harness tick={0} onLoadOlder={onLoadOlder} out={out} />);
    const el = m.container.querySelector<HTMLDivElement>('[data-testid="scroller"]')!;
    installGeometry(el, height);

    el.scrollTop = 120;
    out.hook!.handleTouchStart({ touches: [{ clientY: 10 }], currentTarget: el } as unknown as Parameters<Hook["handleTouchStart"]>[0]);
    out.hook!.handleTouchMove({ touches: [{ clientY: 20 }], currentTarget: el } as unknown as Parameters<Hook["handleTouchMove"]>[0]);
    out.hook!.handleTouchEnd();
    el.scrollTop = 80;
    out.hook!.handleScroll();
    el.scrollTop = 40;
    await act(async () => out.hook!.handleScroll());
    out.hook!.handleScrollEnd();

    expect(onLoadOlder).toHaveBeenCalledTimes(1);
    await m.unmount();
  });

  it("expires wheel authorization at scrollend before later programmatic scrolling", async () => {
    const out: { hook?: Hook } = {};
    const height = { value: 100 };
    const onLoadOlder = vi.fn(async () => {});
    const m = await mount(<Harness tick={0} onLoadOlder={onLoadOlder} out={out} />);
    const el = m.container.querySelector<HTMLDivElement>('[data-testid="scroller"]')!;
    installGeometry(el, height);

    el.scrollTop = 100;
    out.hook!.handleWheel({ deltaY: -1, currentTarget: el } as Parameters<Hook["handleWheel"]>[0]);
    out.hook!.handleScrollEnd();
    el.scrollTop = 0;
    out.hook!.handleScroll();

    expect(onLoadOlder).not.toHaveBeenCalled();
    await m.unmount();
  });

  it("expires authorization on reversal and touch cancellation", async () => {
    const out: { hook?: Hook } = {};
    const height = { value: 100 };
    const onLoadOlder = vi.fn(async () => {});
    const m = await mount(<Harness tick={0} onLoadOlder={onLoadOlder} out={out} />);
    const el = m.container.querySelector<HTMLDivElement>('[data-testid="scroller"]')!;
    installGeometry(el, height);

    el.scrollTop = 100;
    out.hook!.handleWheel({ deltaY: -1, currentTarget: el } as Parameters<Hook["handleWheel"]>[0]);
    el.scrollTop = 120;
    out.hook!.handleScroll();
    el.scrollTop = 0;
    out.hook!.handleScroll();

    el.scrollTop = 100;
    out.hook!.handleTouchStart({ touches: [{ clientY: 10 }], currentTarget: el } as unknown as Parameters<Hook["handleTouchStart"]>[0]);
    out.hook!.handleTouchMove({ touches: [{ clientY: 20 }], currentTarget: el } as unknown as Parameters<Hook["handleTouchMove"]>[0]);
    out.hook!.handleTouchCancel();
    el.scrollTop = 0;
    out.hook!.handleScroll();

    expect(onLoadOlder).not.toHaveBeenCalled();
    await m.unmount();
  });

  it("clears a scrollbar gesture released outside before programmatic scrolling", async () => {
    const out: { hook?: Hook } = {};
    const height = { value: 100 };
    const onLoadOlder = vi.fn(async () => {});
    const m = await mount(<Harness tick={0} onLoadOlder={onLoadOlder} out={out} />);
    const el = m.container.querySelector<HTMLDivElement>('[data-testid="scroller"]')!;
    installGeometry(el, height);
    Object.defineProperty(el, "clientWidth", { configurable: true, value: 90 });
    el.getBoundingClientRect = () => ({ left: 0, top: 0, right: 100, bottom: 100, width: 100, height: 100, x: 0, y: 0, toJSON: () => ({}) });
    el.setPointerCapture = vi.fn();

    el.scrollTop = 100;
    out.hook!.handlePointerDown({ pointerType: "mouse", pointerId: 7, clientX: 95, clientY: 50, currentTarget: el } as Parameters<Hook["handlePointerDown"]>[0]);
    const release = new Event("pointerup");
    Object.defineProperty(release, "pointerId", { value: 7 });
    window.dispatchEvent(release);
    el.scrollTop = 0;
    out.hook!.handleScroll();

    expect(onLoadOlder).not.toHaveBeenCalled();
    await m.unmount();
  });

  it("ignores navigation keys bubbling from interactive descendants", async () => {
    const out: { hook?: Hook } = {};
    const onLoadOlder = vi.fn(async () => {});
    const m = await mount(<Harness tick={0} onLoadOlder={onLoadOlder} out={out} />);
    const el = m.container.querySelector<HTMLDivElement>('[data-testid="scroller"]')!;
    const button = document.createElement("button");
    el.appendChild(button);

    out.hook!.handleKeyDown({ key: "Home", target: button, currentTarget: el } as Parameters<Hook["handleKeyDown"]>[0]);

    expect(onLoadOlder).not.toHaveBeenCalled();
    await m.unmount();
  });

  it("recognizes the left vertical scrollbar in RTL", async () => {
    const out: { hook?: Hook } = {};
    const height = { value: 100 };
    const onLoadOlder = vi.fn(async () => {});
    const m = await mount(<Harness tick={0} onLoadOlder={onLoadOlder} out={out} />);
    const el = m.container.querySelector<HTMLDivElement>('[data-testid="scroller"]')!;
    installGeometry(el, height);
    el.style.direction = "rtl";
    Object.defineProperty(el, "clientWidth", { configurable: true, value: 90 });
    el.getBoundingClientRect = () => ({ left: 0, top: 0, right: 100, bottom: 100, width: 100, height: 100, x: 0, y: 0, toJSON: () => ({}) });
    el.setPointerCapture = vi.fn();

    await act(async () => out.hook!.handlePointerDown({ pointerType: "mouse", pointerId: 8, clientX: 5, clientY: 50, currentTarget: el } as Parameters<Hook["handlePointerDown"]>[0]));

    expect(onLoadOlder).toHaveBeenCalledTimes(1);
    expect(el.setPointerCapture).toHaveBeenCalledWith(8);
    await m.unmount();
  });

  it("discards additional intents while a page is in flight", async () => {
    const out: { hook?: Hook } = {};
    const height = { value: 100 };
    let finish!: () => void;
    const onLoadOlder = vi.fn(() => new Promise<void>((resolve) => { finish = resolve; }));
    const m = await mount(<Harness tick={0} onLoadOlder={onLoadOlder} out={out} />);
    const el = m.container.querySelector<HTMLDivElement>('[data-testid="scroller"]')!;
    installGeometry(el, height);

    out.hook!.handleWheel({ deltaY: -1, currentTarget: el } as Parameters<Hook["handleWheel"]>[0]);
    out.hook!.handleWheel({ deltaY: -1, currentTarget: el } as Parameters<Hook["handleWheel"]>[0]);
    out.hook!.handleKeyDown({ key: "Home", currentTarget: el } as Parameters<Hook["handleKeyDown"]>[0]);
    expect(onLoadOlder).toHaveBeenCalledTimes(1);

    await act(async () => finish());
    out.hook!.handleScroll();
    expect(onLoadOlder).toHaveBeenCalledTimes(1);

    await m.unmount();
  });

  // Regression: under throttled rendering, the DOM grows in a LATER commit
  // than the state change that triggered the load. The restore effect must
  // still fire on that growth commit. A dep-gated effect (the old bug) would
  // run only on the trigger commit, see no growth, bail, and never re-run.
  it("restores position when the prepend lands in a later (throttled) commit", async () => {
    const out: { hook?: Hook } = {};
    const height = { value: 100 };
    // Throttled: the load resolves but the DOM has NOT grown yet — the
    // throttled render data still reflects the pre-prepend list.
    const onLoadOlder = async () => {};

    const m = await mount(<Harness tick={0} onLoadOlder={onLoadOlder} out={out} />);
    const el = m.container.querySelector<HTMLDivElement>('[data-testid="scroller"]')!;
    installGeometry(el, height);

    await act(async () => {
      await out.hook!.triggerLoadOlder();
    });

    // Commit 1 (raw state change): DOM not grown yet — effect must bail
    // without consuming the pending snapshot.
    await m.rerender(<Harness tick={1} onLoadOlder={onLoadOlder} out={out} />);
    expect(el.scrollTop).toBe(0);
    expect(out.hook!.justPrepended.current).toBe(false);

    // Commit 2 (throttled render lands): NOW the DOM grows by 100px.
    height.value = 200;
    await m.rerender(<Harness tick={2} onLoadOlder={onLoadOlder} out={out} />);

    // The fix restores position on this growth commit.
    expect(el.scrollTop).toBe(100);
    expect(out.hook!.justPrepended.current).toBe(true);

    await m.unmount();
  });
});
