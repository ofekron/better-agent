import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, fireEvent, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";

// trackPromise is a passthrough so the real progress store doesn't run; the
// per-test fetch drives the load.
vi.mock("../src/progress/store", () => ({
  trackPromise: <T,>(_op: string, fn: () => Promise<T>) => ({ promise: fn() }),
}));

// Thin native-select stub: drives onChange directly and exposes the
// value/disabled the component passes, without re-testing Select.
vi.mock("../src/components/Select", () => ({
  Select: ({
    value,
    options,
    onChange,
    disabled,
  }: {
    value: string;
    options: { value: string; label: ReactNode }[];
    onChange: (v: string) => void;
    disabled?: boolean;
  }) => (
    <select
      className="bc-select-stub"
      data-disabled={disabled ? "true" : "false"}
      value={value}
      onChange={(e) => onChange((e.target as HTMLSelectElement).value)}
    >
      {options.map((o) => (
        <option key={o.value} value={o.value}>
          {String(o.label)}
        </option>
      ))}
    </select>
  ),
}));

const { publishMock, queueWriteMock } = vi.hoisted(() => ({
  publishMock: vi.fn(),
  queueWriteMock: vi.fn(),
}));
vi.mock("../src/lib/eventBus", () => ({
  eventBus: { publish: publishMock },
}));

vi.mock("../src/utils/writeBacklog", () => ({
  queueWrite: queueWriteMock,
}));

import { SessionTabsSettings } from "../src/components/SessionTabsSettings";

function ok(json: unknown): { json: () => Promise<unknown> } {
  return { json: async () => json };
}

function makeFetch(body: unknown, opts: { reject?: boolean } = {}): ReturnType<typeof vi.fn> {
  return vi.fn(async (url: unknown) => {
    if (opts.reject) throw new Error("load failed");
    void url;
    return ok(body);
  });
}

function selectEl(container: HTMLElement): HTMLSelectElement {
  return container.querySelector("select.bc-select-stub") as HTMLSelectElement;
}
function checkbox(container: HTMLElement): HTMLInputElement {
  return container.querySelector('input[type="checkbox"]') as HTMLInputElement;
}
function optionValues(container: HTMLElement): string[] {
  return [...selectEl(container).querySelectorAll("option")].map((o) => (o as HTMLOptionElement).value);
}

let originalFetch: typeof globalThis.fetch;

beforeEach(() => {
  originalFetch = globalThis.fetch;
  publishMock.mockReset();
  queueWriteMock.mockReset();
});

afterEach(() => {
  globalThis.fetch = originalFetch;
});

describe("SessionTabsSettings — load", () => {
  it("renders defaults before the load resolves and offers all four sort options", () => {
    globalThis.fetch = makeFetch({}, {}) as never;
    const { container } = render(<SessionTabsSettings />);
    expect(checkbox(container).checked).toBe(true);
    expect(selectEl(container).value).toBe("last_opened_at");
    expect(optionValues(container)).toEqual([
      "last_opened_at",
      "tab_joined_at",
      "updated_at",
      "last_user_prompt_at",
    ]);
    // i18n keys render verbatim (no default → key itself).
    expect([...selectEl(container).options].map((o) => o.textContent)).toEqual([
      "session.sortByOpened",
      "session.sortByTabJoined",
      "session.sortByModified",
      "session.sortByUserPrompt",
    ]);
    expect(container.querySelector(".context-strategy-setting")).toBeTruthy();
  });

  it("applies a valid loaded sort and a boolean visibility flag", async () => {
    globalThis.fetch = makeFetch({ sessions_tabs_sort: "updated_at", sessions_tabs_visible: false }) as never;
    const { container } = render(<SessionTabsSettings />);
    await waitFor(() => expect(selectEl(container).value).toBe("updated_at"));
    expect(checkbox(container).checked).toBe(false);
    // select is disabled while not visible
    expect(selectEl(container).getAttribute("data-disabled")).toBe("true");
  });

  it("normalizes an invalid loaded sort back to last_opened_at", async () => {
    globalThis.fetch = makeFetch({ sessions_tabs_sort: "garbage" }) as never;
    const { container } = render(<SessionTabsSettings />);
    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalled());
    expect(selectEl(container).value).toBe("last_opened_at");
  });

  it("ignores a non-boolean visibility flag (typeof guard)", async () => {
    globalThis.fetch = makeFetch({ sessions_tabs_visible: "yes" }) as never;
    const { container } = render(<SessionTabsSettings />);
    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalled());
    expect(checkbox(container).checked).toBe(true);
  });

  it("keeps defaults when the load body has neither key", async () => {
    globalThis.fetch = makeFetch({}) as never;
    const { container } = render(<SessionTabsSettings />);
    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalled());
    expect(checkbox(container).checked).toBe(true);
    expect(selectEl(container).value).toBe("last_opened_at");
  });

  it("swallows a rejected load and keeps defaults (catch is a no-op)", async () => {
    globalThis.fetch = makeFetch({}, { reject: true }) as never;
    const { container } = render(<SessionTabsSettings />);
    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalled());
    expect(checkbox(container).checked).toBe(true);
    expect(selectEl(container).value).toBe("last_opened_at");
  });
});

describe("SessionTabsSettings — patch (optimistic write-through)", () => {
  it("toggles visibility off: applies locally, publishes the bus fact, queues the PATCH", async () => {
    globalThis.fetch = makeFetch({}) as never;
    const { container } = render(<SessionTabsSettings />);
    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalled());

    fireEvent.click(checkbox(container));

    expect(checkbox(container).checked).toBe(false);
    expect(publishMock).toHaveBeenCalledWith("user_prefs_changed", { sessions_tabs_visible: false });
    expect(queueWriteMock).toHaveBeenCalledTimes(1);
    expect(queueWriteMock).toHaveBeenCalledWith({
      method: "PATCH",
      url: "/api/user-prefs",
      body: { sessions_tabs_visible: false },
      key: "user_prefs:sessions_tabs_visible",
    });
    // now-invisible → select disabled
    expect(selectEl(container).getAttribute("data-disabled")).toBe("true");
  });

  it("toggles visibility back on after loading false", async () => {
    globalThis.fetch = makeFetch({ sessions_tabs_visible: false }) as never;
    const { container } = render(<SessionTabsSettings />);
    await waitFor(() => expect(checkbox(container).checked).toBe(false));

    fireEvent.click(checkbox(container));

    expect(checkbox(container).checked).toBe(true);
    expect(publishMock).toHaveBeenCalledWith("user_prefs_changed", { sessions_tabs_visible: true });
    expect(queueWriteMock).toHaveBeenCalledWith({
      method: "PATCH",
      url: "/api/user-prefs",
      body: { sessions_tabs_visible: true },
      key: "user_prefs:sessions_tabs_visible",
    });
  });

  it("selecting a valid sort applies, publishes, and queues with the sort field key", async () => {
    globalThis.fetch = makeFetch({}) as never;
    const { container } = render(<SessionTabsSettings />);
    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalled());

    fireEvent.change(selectEl(container), { target: { value: "updated_at" } });

    expect(selectEl(container).value).toBe("updated_at");
    expect(publishMock).toHaveBeenCalledWith("user_prefs_changed", { sessions_tabs_sort: "updated_at" });
    expect(queueWriteMock).toHaveBeenCalledWith({
      method: "PATCH",
      url: "/api/user-prefs",
      body: { sessions_tabs_sort: "updated_at" },
      key: "user_prefs:sessions_tabs_sort",
    });
  });

  it("selecting an invalid sort normalizes back to last_opened_at before writing through", async () => {
    globalThis.fetch = makeFetch({}) as never;
    const { container } = render(<SessionTabsSettings />);
    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalled());

    fireEvent.change(selectEl(container), { target: { value: "bogus" } });

    expect(selectEl(container).value).toBe("last_opened_at");
    expect(publishMock).toHaveBeenCalledWith("user_prefs_changed", { sessions_tabs_sort: "last_opened_at" });
    expect(queueWriteMock).toHaveBeenCalledWith({
      method: "PATCH",
      url: "/api/user-prefs",
      body: { sessions_tabs_sort: "last_opened_at" },
      key: "user_prefs:sessions_tabs_sort",
    });
  });
});
