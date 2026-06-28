import { afterEach, describe, expect, it, vi } from "vitest";
import { render, waitFor } from "@testing-library/react";
import { SessionTabs } from "../src/components/SessionTabs";
import { makeSession } from "./fixtures";

function rect(width: number): DOMRect {
  return {
    x: 0,
    y: 0,
    width,
    height: 24,
    top: 0,
    right: width,
    bottom: 24,
    left: 0,
    toJSON: () => ({}),
  } as DOMRect;
}

describe("SessionTabs measured capacity", () => {
  const originalRect = HTMLElement.prototype.getBoundingClientRect;
  const originalClientWidth = Object.getOwnPropertyDescriptor(
    HTMLElement.prototype,
    "clientWidth",
  );

  afterEach(() => {
    HTMLElement.prototype.getBoundingClientRect = originalRect;
    if (originalClientWidth) {
      Object.defineProperty(HTMLElement.prototype, "clientWidth", originalClientWidth);
    }
  });

  it("reports the number of tabs that fit actual rendered widths", async () => {
    let available = 250;
    const widths: Record<string, number> = {
      "sess-1": 100,
      "sess-2": 120,
      "sess-3": 90,
    };
    Object.defineProperty(HTMLElement.prototype, "clientWidth", {
      configurable: true,
      get() {
        return this.classList?.contains("session-tabs") ? available : 0;
      },
    });
    HTMLElement.prototype.getBoundingClientRect = function () {
      if (this.classList?.contains("session-tabs")) return rect(available);
      if (this.classList?.contains("session-tab-wrapper")) {
        return rect(widths[this.getAttribute("data-tab-movement-key") ?? ""] ?? 0);
      }
      return originalRect.call(this);
    };
    const onCapacity = vi.fn();

    render(
      <SessionTabs
        sessions={[
          makeSession({ id: "sess-1", name: "First" }),
          makeSession({ id: "sess-2", name: "Second" }),
          makeSession({ id: "sess-3", name: "Third" }),
        ]}
        providers={[]}
        sortField="last_opened_at"
        onSelect={() => {}}
        onClose={() => {}}
        onMeasuredCapacityChange={onCapacity}
      />,
    );

    await waitFor(() => expect(onCapacity).toHaveBeenLastCalledWith(2));

    available = 400;
    window.dispatchEvent(new Event("resize"));

    await waitFor(() => expect(onCapacity).toHaveBeenLastCalledWith(3));
  });
});
