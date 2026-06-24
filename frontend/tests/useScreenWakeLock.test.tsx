import { render, act } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { useScreenWakeLock } from "../src/hooks/useScreenWakeLock";

type TestSentinel = EventTarget & {
  released: boolean;
  release: ReturnType<typeof vi.fn<() => Promise<void>>>;
};

function Harness() {
  useScreenWakeLock();
  return null;
}

function setVisibilityState(value: DocumentVisibilityState) {
  Object.defineProperty(document, "visibilityState", {
    value,
    configurable: true,
  });
}

async function flushPromises() {
  await act(async () => {
    await Promise.resolve();
  });
}

describe("useScreenWakeLock", () => {
  beforeEach(() => {
    setVisibilityState("visible");
  });

  afterEach(() => {
    Reflect.deleteProperty(navigator, "wakeLock");
  });

  it("requests a screen wake lock while the page is visible", async () => {
    const sentinel = new EventTarget() as TestSentinel;
    sentinel.released = false;
    sentinel.release = vi.fn(async () => {
      sentinel.released = true;
    });
    const request = vi.fn(async () => sentinel);
    Object.defineProperty(navigator, "wakeLock", {
      value: { request },
      configurable: true,
    });

    const rendered = render(<Harness />);
    await flushPromises();

    expect(request).toHaveBeenCalledWith("screen");

    rendered.unmount();
    await flushPromises();
    expect(sentinel.release).toHaveBeenCalledTimes(1);
  });

  it("releases while hidden and reacquires when visible again", async () => {
    const first = new EventTarget() as TestSentinel;
    const second = new EventTarget() as TestSentinel;
    first.released = false;
    second.released = false;
    first.release = vi.fn(async () => {
      first.released = true;
    });
    second.release = vi.fn(async () => {
      second.released = true;
    });
    const request = vi.fn()
      .mockResolvedValueOnce(first)
      .mockResolvedValueOnce(second);
    Object.defineProperty(navigator, "wakeLock", {
      value: { request },
      configurable: true,
    });

    const rendered = render(<Harness />);
    await flushPromises();

    setVisibilityState("hidden");
    document.dispatchEvent(new Event("visibilitychange"));
    await flushPromises();

    expect(first.release).toHaveBeenCalledTimes(1);

    setVisibilityState("visible");
    document.dispatchEvent(new Event("visibilitychange"));
    await flushPromises();

    expect(request).toHaveBeenCalledTimes(2);

    rendered.unmount();
  });

  it("does nothing when the browser does not expose wake lock", async () => {
    render(<Harness />);
    await flushPromises();
  });
});
