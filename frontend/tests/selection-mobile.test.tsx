import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";

// Force isMobileViewport() = true so the InvestigateContextMenu's
// mobile useEffect path runs in the test.
vi.mock("../src/components/MobileActionSheet", async () => {
  const actual = await vi.importActual<typeof import("../src/components/MobileActionSheet")>(
    "../src/components/MobileActionSheet",
  );
  return {
    ...actual,
    isMobileViewport: () => window.innerWidth <= 480,
    useMobileActionSheet: () => ({ show: showSheet, dismiss: vi.fn(), visible: false }),
  };
});

import { InvestigateContextMenu } from "../src/components/InvestigateContextMenu";
import {
  clearMobileHandlers,
  registerMobileHandlers,
} from "../src/contexts/MobileHandlersContext";

const showSheet = vi.hoisted(() => vi.fn());

interface CapturedListener {
  type: string;
  fn: EventListener;
  capture: boolean;
}

let captured: CapturedListener[];
let origAdd: typeof document.addEventListener;
let origRemove: typeof document.removeEventListener;

beforeEach(() => {
  showSheet.mockReset();
  Object.defineProperty(window, "innerWidth", { value: 390, configurable: true });
  document.documentElement.removeAttribute("dir");
  captured = [];
  origAdd = document.addEventListener.bind(document);
  origRemove = document.removeEventListener.bind(document);
  document.addEventListener = ((
    type: string,
    fn: EventListener,
    options?: boolean | AddEventListenerOptions,
  ) => {
    const capture = typeof options === "object" ? !!options.capture : !!options;
    captured.push({ type, fn, capture });
    return origAdd(type, fn, options);
  }) as typeof document.addEventListener;
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  clearMobileHandlers();
  document.documentElement.removeAttribute("dir");
  document.addEventListener = origAdd;
  document.removeEventListener = origRemove;
});

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
    render: async (next: React.ReactNode) => {
      await act(async () => root?.render(next));
    },
    unmount: async () => {
      await act(async () => root?.unmount());
      container.remove();
    },
  };
}

describe("InvestigateContextMenu mobile selection", () => {
  it("does NOT preventDefault on contextmenu when target is a <p> text element", async () => {
    const m = await mount(
      <InvestigateContextMenu onInvestigate={() => {}}>
        <p data-testid="text">Selectable paragraph</p>
      </InvestigateContextMenu>,
    );
    const cm = captured.find((c) => c.type === "contextmenu");
    expect(cm).toBeDefined();
    const target = m.container.querySelector('[data-testid="text"]')!;
    const ev = new Event("contextmenu", { bubbles: true, cancelable: true });
    Object.defineProperty(ev, "target", { value: target, writable: false });
    cm!.fn(ev);
    // Native context menu (and therefore native Android text-selection
    // toolbar) MUST be allowed through.
    expect(ev.defaultPrevented).toBe(false);
    await m.unmount();
  });

  it("DOES preventDefault on contextmenu when target is an <img>", async () => {
    const m = await mount(
      <InvestigateContextMenu onInvestigate={() => {}}>
        <img data-testid="img" src="/x.png" />
      </InvestigateContextMenu>,
    );
    const cm = captured.find((c) => c.type === "contextmenu");
    expect(cm).toBeDefined();
    const target = m.container.querySelector('[data-testid="img"]')!;
    const ev = new Event("contextmenu", { bubbles: true, cancelable: true });
    Object.defineProperty(ev, "target", { value: target, writable: false });
    cm!.fn(ev);
    expect(ev.defaultPrevented).toBe(true);
    await m.unmount();
  });

  it("long-press timer does NOT arm on text targets", async () => {
    vi.useFakeTimers();

    const m = await mount(
      <InvestigateContextMenu onInvestigate={() => {}}>
        <p data-testid="text">Long press me</p>
      </InvestigateContextMenu>,
    );
    const ts = captured.find((c) => c.type === "touchstart");
    expect(ts).toBeDefined();

    const target = m.container.querySelector('[data-testid="text"]')!;
    const touchEvent = {
      target,
      touches: [{ clientX: 10, clientY: 10 }],
    } as unknown as TouchEvent;
    ts!.fn(touchEvent);
    vi.advanceTimersByTime(600); // past LONG_PRESS_MS=500
    expect(showSheet).not.toHaveBeenCalled();
    await m.unmount();
  });

  it("opens the mobile action sheet when long pressing a message surface", async () => {
    vi.useFakeTimers();

    const m = await mount(
      <InvestigateContextMenu onInvestigate={() => {}} activeSessionId="session-a">
        <div data-testid="message" data-message-id="message-a" />
      </InvestigateContextMenu>,
    );
    const ts = captured.find((c) => c.type === "touchstart");
    expect(ts).toBeDefined();

    const target = m.container.querySelector('[data-testid="message"]')!;
    const touchEvent = {
      target,
      touches: [{ clientX: 10, clientY: 10 }],
    } as unknown as TouchEvent;
    ts!.fn(touchEvent);
    vi.advanceTimersByTime(600);

    expect(showSheet).toHaveBeenCalledTimes(1);
    expect(showSheet.mock.calls[0][0].map((item: { id: string }) => item.id))
      .toEqual(["copy-id", "investigate"]);
    await m.unmount();
  });

  it.each([
    ["touchend", [{ clientX: 10, clientY: 10 }]],
    ["touchcancel", [{ clientX: 10, clientY: 10 }]],
    ["touchmove", [{ clientX: 30, clientY: 10 }]],
  ])("rejects an already-queued callback after %s", async (eventType, touches) => {
    const m = await mount(
      <InvestigateContextMenu onInvestigate={() => {}}>
        <div data-testid="surface" data-message-id="message-a" />
      </InvestigateContextMenu>,
    );
    let queued: (() => void) | undefined;
    vi.spyOn(globalThis, "setTimeout").mockImplementation((callback) => {
      queued = callback as () => void;
      return 1 as unknown as ReturnType<typeof setTimeout>;
    });
    vi.spyOn(globalThis, "clearTimeout").mockImplementation(() => undefined);

    const target = m.container.querySelector('[data-testid="surface"]')!;
    captured.find((listener) => listener.type === "touchstart")!.fn({
      target,
      touches: [{ clientX: 10, clientY: 10 }],
    } as unknown as TouchEvent);
    captured.find((listener) => listener.type === eventType)!.fn({
      target,
      touches,
    } as unknown as TouchEvent);

    expect(() => queued?.()).not.toThrow();
    expect(showSheet).not.toHaveBeenCalled();
    await m.unmount();
  });

  it("consumes one valid long press with its immutable coordinates", async () => {
    const rewind = vi.fn();
    registerMobileHandlers({ rewind });
    const m = await mount(
      <InvestigateContextMenu onInvestigate={() => {}}>
        <div className="user-message-box" data-testid="surface" data-message-id="message-a" />
      </InvestigateContextMenu>,
    );
    let queued: (() => void) | undefined;
    vi.spyOn(globalThis, "setTimeout").mockImplementation((callback) => {
      queued = callback as () => void;
      return 1 as unknown as ReturnType<typeof setTimeout>;
    });

    const target = m.container.querySelector('[data-testid="surface"]')!;
    captured.find((listener) => listener.type === "touchstart")!.fn({
      target,
      touches: [{ clientX: 17, clientY: 29 }],
    } as unknown as TouchEvent);
    queued?.();
    queued?.();

    expect(showSheet).toHaveBeenCalledTimes(1);
    const rewindAction = showSheet.mock.calls[0][0].find(
      (item: { id: string }) => item.id === "rewind",
    );
    rewindAction.onClick();
    expect(rewind).toHaveBeenCalledWith("message-a", { x: 17, y: 29 });
    await m.unmount();
  });

  it("isolates a replacement gesture from a previously queued callback", async () => {
    const m = await mount(
      <InvestigateContextMenu onInvestigate={() => {}}>
        <div data-testid="surface" data-message-id="message-a" />
      </InvestigateContextMenu>,
    );
    const queued: Array<() => void> = [];
    vi.spyOn(globalThis, "setTimeout").mockImplementation((callback) => {
      queued.push(callback as () => void);
      return queued.length as unknown as ReturnType<typeof setTimeout>;
    });

    const target = m.container.querySelector('[data-testid="surface"]')!;
    const touchstart = captured.find((listener) => listener.type === "touchstart")!;
    touchstart.fn({ target, touches: [{ clientX: 10, clientY: 10 }] } as unknown as TouchEvent);
    touchstart.fn({ target, touches: [{ clientX: 20, clientY: 20 }] } as unknown as TouchEvent);
    queued[0]();
    expect(showSheet).not.toHaveBeenCalled();
    queued[1]();
    expect(showSheet).toHaveBeenCalledTimes(1);
    await m.unmount();
  });

  it("rejects a queued callback when its target was replaced", async () => {
    const m = await mount(
      <InvestigateContextMenu onInvestigate={() => {}}>
        <div data-testid="surface" data-message-id="message-a" />
      </InvestigateContextMenu>,
    );
    let queued: (() => void) | undefined;
    vi.spyOn(globalThis, "setTimeout").mockImplementation((callback) => {
      queued = callback as () => void;
      return 1 as unknown as ReturnType<typeof setTimeout>;
    });

    const target = m.container.querySelector('[data-testid="surface"]')!;
    captured.find((listener) => listener.type === "touchstart")!.fn({
      target,
      touches: [{ clientX: 10, clientY: 10 }],
    } as unknown as TouchEvent);
    target.replaceWith(document.createElement("div"));

    expect(() => queued?.()).not.toThrow();
    expect(showSheet).not.toHaveBeenCalled();
    await m.unmount();
  });

  it("rejects a connected target moved outside its original owner", async () => {
    const m = await mount(
      <InvestigateContextMenu onInvestigate={() => {}}>
        <div data-testid="surface" data-message-id="message-a" />
      </InvestigateContextMenu>,
    );
    let queued: (() => void) | undefined;
    vi.spyOn(globalThis, "setTimeout").mockImplementation((callback) => {
      queued = callback as () => void;
      return 1 as unknown as ReturnType<typeof setTimeout>;
    });

    const target = m.container.querySelector('[data-testid="surface"]')!;
    captured.find((listener) => listener.type === "touchstart")!.fn({
      target,
      touches: [{ clientX: 10, clientY: 10 }],
    } as unknown as TouchEvent);
    document.body.appendChild(target);

    expect(() => queued?.()).not.toThrow();
    expect(showSheet).not.toHaveBeenCalled();
    target.remove();
    await m.unmount();
  });

  it("rejects a queued callback after the owning component unmounts", async () => {
    const m = await mount(
      <InvestigateContextMenu onInvestigate={() => {}}>
        <div data-testid="surface" data-message-id="message-a" />
      </InvestigateContextMenu>,
    );
    let queued: (() => void) | undefined;
    vi.spyOn(globalThis, "setTimeout").mockImplementation((callback) => {
      queued = callback as () => void;
      return 1 as unknown as ReturnType<typeof setTimeout>;
    });
    vi.spyOn(globalThis, "clearTimeout").mockImplementation(() => undefined);

    const target = m.container.querySelector('[data-testid="surface"]')!;
    captured.find((listener) => listener.type === "touchstart")!.fn({
      target,
      touches: [{ clientX: 10, clientY: 10 }],
    } as unknown as TouchEvent);
    await m.unmount();

    expect(() => queued?.()).not.toThrow();
    expect(showSheet).not.toHaveBeenCalled();
  });

  it("rejects an armed narrow-screen gesture after resizing wide", async () => {
    vi.useFakeTimers();
    const m = await mount(
      <InvestigateContextMenu onInvestigate={() => {}}>
        <div data-testid="surface" data-message-id="message-a" />
      </InvestigateContextMenu>,
    );
    const target = m.container.querySelector('[data-testid="surface"]')!;
    captured.find((listener) => listener.type === "touchstart")!.fn({
      target,
      touches: [{ clientX: 10, clientY: 10 }],
    } as unknown as TouchEvent);

    Object.defineProperty(window, "innerWidth", { value: 700, configurable: true });
    window.dispatchEvent(new Event("resize"));
    vi.advanceTimersByTime(600);

    expect(showSheet).not.toHaveBeenCalled();
    await m.unmount();
  });

  it("invalidates an armed gesture as soon as a second finger appears", async () => {
    const m = await mount(
      <InvestigateContextMenu onInvestigate={() => {}}>
        <div data-testid="surface" data-message-id="message-a" />
      </InvestigateContextMenu>,
    );
    let queued: (() => void) | undefined;
    vi.spyOn(globalThis, "setTimeout").mockImplementation((callback) => {
      queued = callback as () => void;
      return 1 as unknown as ReturnType<typeof setTimeout>;
    });
    vi.spyOn(globalThis, "clearTimeout").mockImplementation(() => undefined);

    const target = m.container.querySelector('[data-testid="surface"]')!;
    const touchstart = captured.find((listener) => listener.type === "touchstart")!;
    touchstart.fn({
      target,
      touches: [{ identifier: 7, clientX: 10, clientY: 10 }],
    } as unknown as TouchEvent);
    touchstart.fn({
      target,
      touches: [
        { identifier: 7, clientX: 10, clientY: 10 },
        { identifier: 8, clientX: 20, clientY: 20 },
      ],
    } as unknown as TouchEvent);

    expect(() => queued?.()).not.toThrow();
    expect(showSheet).not.toHaveBeenCalled();
    await m.unmount();
  });

  it("ignores a touchend for a different finger", async () => {
    const m = await mount(
      <InvestigateContextMenu onInvestigate={() => {}}>
        <div data-testid="surface" data-message-id="message-a" />
      </InvestigateContextMenu>,
    );
    let queued: (() => void) | undefined;
    vi.spyOn(globalThis, "setTimeout").mockImplementation((callback) => {
      queued = callback as () => void;
      return 1 as unknown as ReturnType<typeof setTimeout>;
    });

    const target = m.container.querySelector('[data-testid="surface"]')!;
    captured.find((listener) => listener.type === "touchstart")!.fn({
      target,
      touches: [{ identifier: 7, clientX: 10, clientY: 10 }],
    } as unknown as TouchEvent);
    captured.find((listener) => listener.type === "touchend")!.fn({
      target,
      touches: [{ identifier: 7, clientX: 10, clientY: 10 }],
      changedTouches: [{ identifier: 8, clientX: 20, clientY: 20 }],
    } as unknown as TouchEvent);
    queued?.();

    expect(showSheet).toHaveBeenCalledTimes(1);
    await m.unmount();
  });

  it.each([
    ["data-message-id", (target: Element) => target.setAttribute("data-message-id", "message-b")],
    ["message classes", (target: Element) => target.classList.add("user-message-box")],
  ])("rejects a same-node %s mutation before timeout", async (_label, mutate) => {
    const m = await mount(
      <InvestigateContextMenu onInvestigate={() => {}} activeSessionId="session-a">
        <div data-testid="surface" data-message-id="message-a" />
      </InvestigateContextMenu>,
    );
    let queued: (() => void) | undefined;
    vi.spyOn(globalThis, "setTimeout").mockImplementation((callback) => {
      queued = callback as () => void;
      return 1 as unknown as ReturnType<typeof setTimeout>;
    });

    const target = m.container.querySelector('[data-testid="surface"]')!;
    captured.find((listener) => listener.type === "touchstart")!.fn({
      target,
      touches: [{ identifier: 7, clientX: 10, clientY: 10 }],
    } as unknown as TouchEvent);
    mutate(target);

    expect(() => queued?.()).not.toThrow();
    expect(showSheet).not.toHaveBeenCalled();
    await m.unmount();
  });

  it("rejects an armed gesture after the active session changes", async () => {
    const child = <div data-testid="surface" data-message-id="message-a" />;
    const onInvestigate = vi.fn();
    const m = await mount(
      <InvestigateContextMenu onInvestigate={onInvestigate} activeSessionId="session-a">
        {child}
      </InvestigateContextMenu>,
    );
    let queued: (() => void) | undefined;
    vi.spyOn(globalThis, "setTimeout").mockImplementation((callback) => {
      queued = callback as () => void;
      return 1 as unknown as ReturnType<typeof setTimeout>;
    });

    const target = m.container.querySelector('[data-testid="surface"]')!;
    captured.find((listener) => listener.type === "touchstart")!.fn({
      target,
      touches: [{ identifier: 7, clientX: 10, clientY: 10 }],
    } as unknown as TouchEvent);
    await m.render(
      <InvestigateContextMenu onInvestigate={onInvestigate} activeSessionId="session-b">
        {child}
      </InvestigateContextMenu>,
    );

    expect(() => queued?.()).not.toThrow();
    expect(showSheet).not.toHaveBeenCalled();
    await m.unmount();
  });

  it("reacts to narrow RTL viewport changes without remounting", async () => {
    Object.defineProperty(window, "innerWidth", { value: 700, configurable: true });
    document.documentElement.setAttribute("dir", "rtl");
    vi.useFakeTimers();
    const m = await mount(
      <InvestigateContextMenu onInvestigate={() => {}}>
        <div data-testid="surface" data-message-id="message-a" />
      </InvestigateContextMenu>,
    );
    const target = m.container.querySelector('[data-testid="surface"]')!;
    const touchstart = captured.find((listener) => listener.type === "touchstart")!;
    touchstart.fn({ target, touches: [{ clientX: 10, clientY: 10 }] } as unknown as TouchEvent);
    vi.advanceTimersByTime(600);
    expect(showSheet).not.toHaveBeenCalled();

    Object.defineProperty(window, "innerWidth", { value: 320, configurable: true });
    window.dispatchEvent(new Event("resize"));
    touchstart.fn({ target, touches: [{ clientX: 10, clientY: 10 }] } as unknown as TouchEvent);
    vi.advanceTimersByTime(600);
    expect(showSheet).toHaveBeenCalledTimes(1);
    await m.unmount();
  });
});
