import { describe, expect, it, vi } from "vitest";
import {
  requestMessageFocus,
  peekPendingMessageFocus,
  clearPendingMessageFocus,
  scrollMessageIntoView,
} from "../src/utils/messageFocus";
import { eventBus } from "../src/lib/eventBus";

// The module holds a process-level `pending` target across calls; isolate
// each test by importing fresh so a prior request never leaks in.
async function fresh() {
  vi.resetModules();
  return await import("../src/utils/messageFocus");
}

describe("messageFocus pending-target state", () => {
  it("request sets a peekable target scoped to its session", async () => {
    const m = await fresh();
    m.requestMessageFocus("s1", "m1");
    expect(m.peekPendingMessageFocus("s1")).toBe("m1");
    // Different session sees nothing.
    expect(m.peekPendingMessageFocus("s2")).toBeNull();
  });

  it("clear removes the target only when the message id matches", async () => {
    const m = await fresh();
    m.requestMessageFocus("s1", "m1");
    m.clearPendingMessageFocus("other");
    expect(m.peekPendingMessageFocus("s1")).toBe("m1");
    m.clearPendingMessageFocus("m1");
    expect(m.peekPendingMessageFocus("s1")).toBeNull();
  });

  it("clear is a safe no-op when nothing is pending", async () => {
    const m = await fresh();
    expect(() => m.clearPendingMessageFocus("nope")).not.toThrow();
    expect(m.peekPendingMessageFocus("any")).toBeNull();
  });

  it("request publishes a focus_message event with the session/message ids", () => {
    const seen: unknown[] = [];
    const off = eventBus.subscribe("focus_message", (p) => seen.push(p));
    try {
      requestMessageFocus("s9", "m9");
    } finally {
      off();
    }
    expect(seen).toHaveLength(1);
    expect(seen[0]).toEqual({ session_id: "s9", message_id: "m9" });
  });
});

describe("scrollMessageIntoView", () => {
  /** Build a fake DOM: a scroll container with a positioned target inside. */
  function fakeDoc(opts: { container?: HTMLElement | null; target?: HTMLElement | null }) {
    const target = opts.target === undefined ? makeEl({ top: 50 }) : opts.target;
    const container =
      opts.container === undefined
        ? makeEl({ top: 10, scrollTop: 100, target })
        : opts.container;
    return {
      doc: { querySelector: (sel: string) => (sel === ".chat-messages" ? container : null) },
      container,
      target,
    };
  }

  function makeEl(overrides: {
    top?: number;
    scrollTop?: number;
    target?: HTMLElement | null;
  } = {}): HTMLElement {
    const animate = vi.fn();
    const scrollTo = vi.fn();
    const target = overrides.target ?? null;
    const el = {
      getBoundingClientRect: () => ({ top: overrides.top ?? 0 }),
      scrollTop: overrides.scrollTop ?? 0,
      scrollTo,
      animate,
      querySelector: () => target,
    } as unknown as HTMLElement;
    return el;
  }

  it("returns false when the chat container is not in the document", () => {
    const { doc } = fakeDoc({ container: null });
    expect(scrollMessageIntoView("m", doc as unknown as Document)).toBe(false);
  });

  it("returns false when the target message is not yet rendered", () => {
    const container = makeEl({ top: 10, scrollTop: 100, target: null });
    const { doc } = fakeDoc({ container });
    expect(scrollMessageIntoView("m", doc as unknown as Document)).toBe(false);
  });

  it("scrolls the target into view with a 24px offset and returns true", () => {
    const { doc, container } = fakeDoc({});
    // scrollTop(100) + targetTop(50) - containerTop(10) - 24 = 116
    expect(scrollMessageIntoView("m", doc as unknown as Document)).toBe(true);
    expect((container as unknown as { scrollTo: ReturnType<typeof vi.fn> }).scrollTo)
      .toHaveBeenCalledWith({ top: 116, behavior: "smooth" });
  });

  it("flashes the target via the Web Animations API", () => {
    const { doc, target } = fakeDoc({});
    scrollMessageIntoView("m", doc as unknown as Document);
    const animate = (target as unknown as { animate: ReturnType<typeof vi.fn> }).animate;
    expect(animate).toHaveBeenCalledTimes(1);
    const [keyframes, opts] = animate.mock.calls[0];
    expect(keyframes).toHaveLength(3);
    expect(opts).toMatchObject({ duration: 1500, easing: "ease-out" });
  });

  it("tolerates an unsupported animate() (scroll alone still lands)", () => {
    const target = makeEl({ top: 50 });
    (target as unknown as { animate: () => never }).animate = () => {
      throw new Error("animate unsupported");
    };
    const container = makeEl({ top: 10, scrollTop: 100, target });
    const doc = { querySelector: (sel: string) => (sel === ".chat-messages" ? container : null) };
    expect(() => scrollMessageIntoView("m", doc as unknown as Document)).not.toThrow();
  });
});
