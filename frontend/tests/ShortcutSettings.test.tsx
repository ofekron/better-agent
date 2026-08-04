import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, waitFor } from "@testing-library/react";

// --- hoisted control plane --------------------------------------------------

const fetchFn = vi.hoisted(() => ({ current: vi.fn() }));

// trackPromise is reduced to a plain fn() call so the store/WS machinery is
// not pulled in; the component only consumes `.promise`.
vi.mock("../src/progress/store", () => ({
  trackPromise: (_opId: string, fn: () => Promise<unknown>) => ({ promise: fn() }),
}));

vi.mock("../src/api", () => ({ API: "http://test" }));

import { ShortcutSettings } from "../src/components/ShortcutSettings";

// --- fetch stub -------------------------------------------------------------

type FetchResp = {
  ok: boolean;
  status: number;
  json: () => Promise<unknown>;
};

function jsonRes(body: unknown, ok = true): FetchResp {
  return { ok, status: ok ? 200 : 500, json: () => Promise.resolve(body) };
}

const DEFAULTS = ["TLDR", "Didn't read, but I trust you go ahead", "/Adv", "Confirmed Go ahead"];

function setFetch(impl: (url: string, init?: RequestInit) => Promise<FetchResp>): void {
  fetchFn.current = vi.fn(impl) as unknown as typeof fetch;
  globalThis.fetch = fetchFn.current as unknown as typeof fetch;
}

function shortcuts(container: HTMLElement): string[] {
  return Array.from(container.querySelectorAll(".shortcut-settings-text")).map(
    (n) => n.textContent ?? "",
  );
}

function clickAdd(container: HTMLElement): void {
  fireEvent.click(container.querySelector(".shortcut-settings-add .btn-secondary")!);
}

function clickRemove(container: HTMLElement, index: number): void {
  const btns = container.querySelectorAll(".shortcut-settings-remove");
  fireEvent.click(btns[index]!);
}

function clickReset(container: HTMLElement): void {
  fireEvent.click(container.querySelector(".shortcut-settings-header .btn-secondary")!);
}

function inputEl(container: HTMLElement): HTMLInputElement {
  return container.querySelector(".shortcut-settings-add input") as HTMLInputElement;
}

function patchBody(init?: RequestInit): { shortcut_responses?: string[] } | null {
  if (!init?.body) return null;
  try {
    return JSON.parse(init.body as string);
  } catch {
    return null;
  }
}

// --- tests ------------------------------------------------------------------

describe("ShortcutSettings", () => {
  let dispatchSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    dispatchSpy = vi.spyOn(window, "dispatchEvent");
    setFetch(async () => jsonRes({})); // load: no shortcut_responses
  });

  afterEach(() => {
    dispatchSpy.mockRestore();
    vi.restoreAllMocks();
  });

  it("renders DEFAULTS while the load response omits shortcut_responses", () => {
    const { container } = render(<ShortcutSettings />);
    expect(shortcuts(container)).toEqual(DEFAULTS);
  });

  it("replaces the list when the load response carries shortcut_responses", async () => {
    const remote = ["Alpha", "Beta"];
    setFetch(async () => jsonRes({ shortcut_responses: remote }));
    const { container } = render(<ShortcutSettings />);
    await waitFor(() => expect(shortcuts(container)).toEqual(remote));
  });

  it("swallows a load error and keeps DEFAULTS", async () => {
    setFetch(async () => {
      throw new Error("boom");
    });
    const { container } = render(<ShortcutSettings />);
    // give the rejected promise a tick to settle
    await act(async () => {
      await Promise.resolve();
    });
    expect(shortcuts(container)).toEqual(DEFAULTS);
  });

  it("does nothing on Enter with an empty input", () => {
    const { container } = render(<ShortcutSettings />);
    const callsBefore = (globalThis.fetch as unknown as ReturnType<typeof vi.fn>).mock.calls.length;
    fireEvent.keyDown(inputEl(container), { key: "Enter" });
    expect(shortcuts(container)).toEqual(DEFAULTS);
    expect((globalThis.fetch as unknown as ReturnType<typeof vi.fn>).mock.calls.length).toBe(
      callsBefore,
    );
  });

  it("ignores non-Enter keys", () => {
    const { container } = render(<ShortcutSettings />);
    fireEvent.keyDown(inputEl(container), { key: "a" });
    fireEvent.keyDown(inputEl(container), { key: "Escape" });
    // still DEFAULTS, no save fired
    expect(shortcuts(container)).toEqual(DEFAULTS);
  });

  it("skips duplicates on add", async () => {
    setFetch(async () => jsonRes({}));
    const { container } = render(<ShortcutSettings />);
    await waitFor(() => expect(shortcuts(container)).toEqual(DEFAULTS));

    const input = inputEl(container);
    fireEvent.change(input, { target: { value: "TLDR" } });
    fireEvent.keyDown(input, { key: "Enter" });

    // no save PATCH dispatched, list unchanged
    expect(shortcuts(container)).toEqual(DEFAULTS);
  });

  it("adds a new shortcut via Enter, saves, and broadcasts the change", async () => {
    const seq: FetchResp[] = [
      jsonRes({}), // load
      jsonRes({ ok: true }), // save PATCH
    ];
    let i = 0;
    setFetch(async () => seq[i++]!);
    const { container } = render(<ShortcutSettings />);

    const input = inputEl(container);
    fireEvent.change(input, { target: { value: "  Ship it  " } });
    fireEvent.keyDown(input, { key: "Enter" });

    await waitFor(() => expect(shortcuts(container)).toEqual([...DEFAULTS, "Ship it"]));
    expect(input.value).toBe("");
    // the save PATCH carried the appended list
    const patch = (globalThis.fetch as unknown as ReturnType<typeof vi.fn>).mock.calls.find(
      (c) => patchBody(c[1] as RequestInit | undefined) !== null,
    );
    expect(patchBody(patch?.[1] as RequestInit)).toEqual({
      shortcut_responses: [...DEFAULTS, "Ship it"],
    });
    // broadcast fired with the updated list
    expect(dispatchSpy).toHaveBeenCalledWith(
      expect.objectContaining({ type: "shortcut_responses_changed", detail: [...DEFAULTS, "Ship it"] }),
    );
  });

  it("adds via the + button (disabled-state branch: enabled when trimmed present)", async () => {
    const seq: FetchResp[] = [jsonRes({}), jsonRes({ ok: true })];
    let i = 0;
    setFetch(async () => seq[i++]!);
    const { container } = render(<ShortcutSettings />);

    const input = inputEl(container);
    fireEvent.change(input, { target: { value: "Go" } });
    clickAdd(container);

    await waitFor(() => expect(shortcuts(container)).toEqual([...DEFAULTS, "Go"]));
  });

  it("removes a shortcut by index, saves, and broadcasts", async () => {
    const seq: FetchResp[] = [jsonRes({}), jsonRes({ ok: true })];
    let i = 0;
    setFetch(async () => seq[i++]!);
    const { container } = render(<ShortcutSettings />);

    await waitFor(() => expect(shortcuts(container)).toEqual(DEFAULTS));
    clickRemove(container, 1); // remove "Didn't read..."

    const expected = DEFAULTS.filter((_, idx) => idx !== 1);
    await waitFor(() => expect(shortcuts(container)).toEqual(expected));
    const patch = (globalThis.fetch as unknown as ReturnType<typeof vi.fn>).mock.calls.find(
      (c) => patchBody(c[1] as RequestInit | undefined) !== null,
    );
    expect(patchBody(patch?.[1] as RequestInit)).toEqual({ shortcut_responses: expected });
  });

  it("resetDefaults restores DEFAULTS via save", async () => {
    // load with a custom list, then reset to defaults
    const remote = ["Only One"];
    const seq: FetchResp[] = [
      jsonRes({ shortcut_responses: remote }),
      jsonRes({ ok: true }),
    ];
    let i = 0;
    setFetch(async () => seq[i++]!);
    const { container } = render(<ShortcutSettings />);

    await waitFor(() => expect(shortcuts(container)).toEqual(remote));
    clickReset(container);

    await waitFor(() => expect(shortcuts(container)).toEqual([...DEFAULTS]));
    const patch = (globalThis.fetch as unknown as ReturnType<typeof vi.fn>).mock.calls.find(
      (c) => patchBody(c[1] as RequestInit | undefined) !== null,
    );
    expect(patchBody(patch?.[1] as RequestInit)).toEqual({
      shortcut_responses: [...DEFAULTS],
    });
  });

  it("disable button is disabled until a trimmed value exists", () => {
    const { container } = render(<ShortcutSettings />);
    const addBtn = container.querySelector(
      ".shortcut-settings-add .btn-secondary",
    ) as HTMLButtonElement;
    expect(addBtn.disabled).toBe(true);
    // whitespace-only still disabled
    fireEvent.change(inputEl(container), { target: { value: "   " } });
    expect(addBtn.disabled).toBe(true);
    // real value enables
    fireEvent.change(inputEl(container), { target: { value: "x" } });
    expect(addBtn.disabled).toBe(false);
  });

  it("save failure: keeps list, sets saving false, does not broadcast", async () => {
    let saveSeen = false;
    setFetch(async (url, init) => {
      // load: return a custom list; save PATCH: throw to exercise the catch branch
      if (init?.method === "PATCH") {
        saveSeen = true;
        throw new Error("network");
      }
      return jsonRes({ shortcut_responses: ["Keep"] });
    });
    const { container } = render(<ShortcutSettings />);
    await waitFor(() => expect(shortcuts(container)).toEqual(["Keep"]));

    fireEvent.change(inputEl(container), { target: { value: "New" } });
    clickAdd(container);

    // failure path: list unchanged (still ["Keep"]) once saving settles
    await waitFor(() => {
      const addBtn = container.querySelector(
        ".shortcut-settings-add .btn-secondary",
      ) as HTMLButtonElement;
      return !addBtn.disabled; // re-enabled after finally
    });
    expect(shortcuts(container)).toEqual(["Keep"]);
    expect(saveSeen).toBe(true);
    // no broadcast on failure
    expect(dispatchSpy).not.toHaveBeenCalledWith(
      expect.objectContaining({ type: "shortcut_responses_changed" }),
    );
  });
});
