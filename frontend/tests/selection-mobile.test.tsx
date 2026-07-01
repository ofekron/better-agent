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
    isMobileViewport: () => true,
    useMobileActionSheet: () => ({ show: vi.fn(), dismiss: vi.fn(), visible: false }),
  };
});

import { InvestigateContextMenu } from "../src/components/InvestigateContextMenu";

interface CapturedListener {
  type: string;
  fn: EventListener;
  capture: boolean;
}

let captured: CapturedListener[];
let origAdd: typeof document.addEventListener;
let origRemove: typeof document.removeEventListener;

beforeEach(() => {
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
    const showSheet = vi.fn();
    const mocked = await import("../src/components/MobileActionSheet");
    vi.spyOn(mocked, "useMobileActionSheet").mockReturnValue({
      show: showSheet,
      dismiss: vi.fn(),
      visible: false,
    });

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
    vi.useRealTimers();
    await m.unmount();
  });

  it("opens the mobile action sheet when long pressing a message surface", async () => {
    vi.useFakeTimers();
    const showSheet = vi.fn();
    const mocked = await import("../src/components/MobileActionSheet");
    vi.spyOn(mocked, "useMobileActionSheet").mockReturnValue({
      show: showSheet,
      dismiss: vi.fn(),
      visible: false,
    });

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
    vi.useRealTimers();
    await m.unmount();
  });
});
