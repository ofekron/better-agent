import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, waitFor } from "@testing-library/react";

// --- hoisted control plane --------------------------------------------------

const fetchFn = vi.hoisted(() => ({ current: vi.fn() }));

vi.mock("react-i18next", () => {
  // Surface interpolation args so owner-label assertions can read the name.
  const t = (k: string, opts?: Record<string, unknown>) =>
    opts && typeof opts.name === "string" ? `${k}:${opts.name}` : k;
  return { useTranslation: () => ({ t }) };
});

vi.mock("src/api", () => ({ API: "http://test" }));

// trackPromise passthrough — we only need `{ promise: fn() }`.
vi.mock("src/progress/store", () => ({
  trackPromise: <T,>(_op: string, fn: () => Promise<T>) => ({ promise: fn() }),
}));

import {
  ExtensionAppSettingsSection,
} from "src/components/ExtensionAppSettingsSection";
import type {
  ExtensionAppSettingItem,
  ExtensionAppSettingsSection as SectionDescriptor,
} from "src/hooks/useExtensionAppSettings";

// --- fetch stub -------------------------------------------------------------

type Res = { ok: boolean; status: number; json: () => Promise<unknown> };
const res = (ok: boolean, status = ok ? 200 : 500): Res => ({
  ok,
  status,
  json: () => Promise.resolve({}),
});

function installFetch(
  impl: (url: string, init?: RequestInit) => Promise<Res>,
) {
  fetchFn.current = vi.fn(impl) as unknown as typeof fetchFn.current;
  globalThis.fetch = ((...args: Parameters<typeof fetch>) =>
    fetchFn.current(...args)) as unknown as typeof fetch;
}

// --- fixtures ---------------------------------------------------------------

function makeItem(
  overrides: Partial<ExtensionAppSettingItem>,
): ExtensionAppSettingItem {
  return {
    extension_id: "ext1",
    extension_name: "Ext One",
    key: "k1",
    label: "Label",
    type: "string",
    enum: [],
    help: "",
    default: "",
    value: "",
    ...overrides,
  };
}

function makeSection(
  items: ExtensionAppSettingItem[],
  description = "",
): SectionDescriptor {
  return { id: "sec", label: "Sec", description, items };
}

function onSavedSpy() {
  return vi.fn().mockResolvedValue(undefined);
}

// The PATCH body shape the component must send.
function patchCalls() {
  return fetchFn.current.mock.calls.filter(
    (c) => (c[1] as RequestInit | undefined)?.method === "PATCH",
  );
}

function patchBody(): { key: string; value: unknown } | undefined {
  const call = patchCalls().at(-1);
  const body = (call?.[1] as RequestInit | undefined)?.body;
  return body ? (JSON.parse(body as string) as { key: string; value: unknown }) : undefined;
}

// --- tests ------------------------------------------------------------------

describe("ExtensionAppSettingsSection", () => {
  beforeEach(() => {
    installFetch(() => Promise.resolve(res(true)));
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders a description hint when present and omits it when absent", () => {
    const { container: withDesc } = render(
      <ExtensionAppSettingsSection
        section={makeSection([makeItem({ label: "x" })], "a hint")}
        onSaved={onSavedSpy()}
      />,
    );
    const hint = withDesc.querySelector(".settings-hint");
    expect(hint?.textContent).toBe("a hint");

    const { container: noDesc } = render(
      <ExtensionAppSettingsSection
        section={makeSection([makeItem({ label: "y" })], "")}
        onSaved={onSavedSpy()}
      />,
    );
    expect(noDesc.querySelector(".settings-hint")).toBeNull();
  });

  it("renders a boolean item as a checkbox reflecting its value", () => {
    const { container: onBox } = render(
      <ExtensionAppSettingsSection
        section={makeSection([
          makeItem({ type: "boolean", key: "b", value: true }),
        ])}
        onSaved={onSavedSpy()}
      />,
    );
    const onCheckbox = onBox.querySelector(
      "input[type=checkbox]",
    ) as HTMLInputElement;
    expect(onCheckbox.checked).toBe(true);

    const { container: offBox } = render(
      <ExtensionAppSettingsSection
        section={makeSection([
          makeItem({ type: "boolean", key: "b", value: false }),
        ])}
        onSaved={onSavedSpy()}
      />,
    );
    const offCheckbox = offBox.querySelector(
      "input[type=checkbox]",
    ) as HTMLInputElement;
    expect(offCheckbox.checked).toBe(false);
  });

  it("saves the toggled boolean value on change and calls onSaved on success", async () => {
    const onSaved = onSavedSpy();
    const { container } = render(
      <ExtensionAppSettingsSection
        section={makeSection([
          makeItem({
            extension_id: "ext-bool",
            type: "boolean",
            key: "b",
            value: true,
          }),
        ])}
        onSaved={onSaved}
      />,
    );
    const checkbox = container.querySelector(
      "input[type=checkbox]",
    ) as HTMLInputElement;

    await act(async () => {
      fireEvent.click(checkbox);
    });

    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
    expect(patchBody()).toEqual({ key: "b", value: false });
    const url = patchCalls().at(-1)![0];
    expect(url).toBe("http://test/api/extensions/ext-bool/settings");
  });

  it("renders a numeric enum as a select and saves the numeric choice", async () => {
    const onSaved = onSavedSpy();
    const { container } = render(
      <ExtensionAppSettingsSection
        section={makeSection([
          makeItem({
            type: "number",
            key: "n",
            enum: [1, 2, 3],
            value: 1,
          }),
        ])}
        onSaved={onSaved}
      />,
    );
    const select = container.querySelector("select") as HTMLSelectElement;
    expect(select.value).toBe("1");
    expect(select.querySelectorAll("option")).toHaveLength(3);

    await act(async () => {
      fireEvent.change(select, { target: { value: "2" } });
    });

    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    // number enum -> Number(value)
    expect(patchBody()).toEqual({ key: "n", value: 2 });
  });

  it("renders a string enum as a select and saves the string choice", async () => {
    const onSaved = onSavedSpy();
    const { container } = render(
      <ExtensionAppSettingsSection
        section={makeSection([
          makeItem({
            type: "string",
            key: "s",
            enum: ["a", "b"],
            value: "a",
          }),
        ])}
        onSaved={onSaved}
      />,
    );

    const select = container.querySelector("select") as HTMLSelectElement;
    await act(async () => {
      fireEvent.change(select, { target: { value: "b" } });
    });

    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(patchBody()).toEqual({ key: "s", value: "b" });
  });

  it("renders a text input with help, skips save when unchanged, saves when changed", async () => {
    const onSaved = onSavedSpy();
    const { container } = render(
      <ExtensionAppSettingsSection
        section={makeSection([
          makeItem({
            type: "string",
            key: "t",
            value: "hello",
            help: "a helpful tip",
          }),
        ])}
        onSaved={onSaved}
      />,
    );

    const input = container.querySelector("input[type=text]") as HTMLInputElement;
    expect(input.value).toBe("hello");
    expect(container.querySelector(".settings-hint")?.textContent).toBe(
      "a helpful tip",
    );

    // Blur without changing -> no save.
    await act(async () => {
      fireEvent.blur(input);
    });
    expect(patchCalls()).toHaveLength(0);

    // Change then blur -> save the new string.
    await act(async () => {
      fireEvent.change(input, { target: { value: "world" } });
    });
    await act(async () => {
      fireEvent.blur(input);
    });
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(patchBody()).toEqual({ key: "t", value: "world" });
  });

  it("renders a number input, skips save when unchanged, saves the numeric value", async () => {
    const onSaved = onSavedSpy();
    const { container } = render(
      <ExtensionAppSettingsSection
        section={makeSection([
          makeItem({ type: "number", key: "num", value: 5 }),
        ])}
        onSaved={onSaved}
      />,
    );

    const input = container.querySelector("input[type=number]") as HTMLInputElement;
    expect(input.value).toBe("5");

    // Blur unchanged -> Number("5") === 5 -> skip.
    await act(async () => {
      fireEvent.blur(input);
    });
    expect(patchCalls()).toHaveLength(0);

    await act(async () => {
      fireEvent.change(input, { target: { value: "7" } });
    });
    await act(async () => {
      fireEvent.blur(input);
    });
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(patchBody()).toEqual({ key: "num", value: 7 });
  });

  it("shows failed status and skips onSaved when PATCH returns not ok", async () => {
    installFetch(() => Promise.resolve(res(false, 500)));
    const onSaved = onSavedSpy();
    const { container } = render(
      <ExtensionAppSettingsSection
        section={makeSection([
          makeItem({ type: "boolean", key: "b", value: false }),
        ])}
        onSaved={onSaved}
      />,
    );
    const checkbox = container.querySelector(
      "input[type=checkbox]",
    ) as HTMLInputElement;

    await act(async () => {
      fireEvent.click(checkbox);
    });

    await waitFor(() =>
      expect(
        container.querySelector(".extension-app-settings-status.is-error"),
      ).not.toBeNull(),
    );
    expect(container.textContent).toContain(
      "settings.extensionAppSettingsFailed",
    );
    expect(onSaved).not.toHaveBeenCalled();
    // finally re-enabled the control.
    expect(checkbox.disabled).toBe(false);
  });

  it("shows failed status on a network error (fetch rejects)", async () => {
    installFetch(() => Promise.reject(new Error("offline")));
    const onSaved = onSavedSpy();
    const { container } = render(
      <ExtensionAppSettingsSection
        section={makeSection([
          makeItem({ type: "boolean", key: "b", value: false }),
        ])}
        onSaved={onSaved}
      />,
    );
    const checkbox = container.querySelector(
      "input[type=checkbox]",
    ) as HTMLInputElement;

    await act(async () => {
      fireEvent.click(checkbox);
    });

    await waitFor(() =>
      expect(
        container.querySelector(".extension-app-settings-status.is-error"),
      ).not.toBeNull(),
    );
    expect(onSaved).not.toHaveBeenCalled();
  });

  it("shows saving status and disables the control while PATCH is in flight", async () => {
    let releasePatch: ((r: Res) => void) | null = null;
    installFetch(
      () =>
        new Promise<Res>((resolve) => {
          releasePatch = resolve;
        }),
    );
    const onSaved = onSavedSpy();
    const { container } = render(
      <ExtensionAppSettingsSection
        section={makeSection([
          makeItem({ type: "boolean", key: "b", value: false }),
        ])}
        onSaved={onSaved}
      />,
    );
    const checkbox = container.querySelector(
      "input[type=checkbox]",
    ) as HTMLInputElement;

    await act(async () => {
      fireEvent.click(checkbox);
    });

    // In-flight: saving label + disabled control.
    await waitFor(() => expect(checkbox.disabled).toBe(true));
    expect(container.textContent).toContain(
      "settings.extensionAppSettingsSaving",
    );
    expect(
      container.querySelector(".extension-app-settings-status"),
    ).not.toBeNull();

    // Release success -> onSaved called, status clears, re-enabled.
    await act(async () => {
      releasePatch!(res(true));
    });
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(checkbox.disabled).toBe(false);
    expect(
      container.querySelector(".extension-app-settings-status"),
    ).toBeNull();
  });

  it("renders help hints for every control variant and tolerates a null value", () => {
    const { container } = render(
      <ExtensionAppSettingsSection
        section={makeSection([
          // boolean control WITH help
          makeItem({ type: "boolean", key: "b", value: true, help: "bool-help" }),
          // enum control WITH help AND a null value (covers ?? fallback + help)
          makeItem({ type: "number", key: "e", enum: [1, 2], value: null, help: "enum-help" }),
          // plain control with a null value (covers ?? fallback)
          makeItem({ type: "string", key: "t", value: null }),
        ])}
        onSaved={onSavedSpy()}
      />,
    );

    const hints = Array.from(container.querySelectorAll(".settings-hint")).map(
      (n) => n.textContent,
    );
    expect(hints).toContain("bool-help");
    expect(hints).toContain("enum-help");

    // null value -> String(null ?? "") === "" for the plain input (the
    // select still renders its enum options; the branch we cover is the
    // `?? ""` fallback being evaluated, not the DOM's option reflection).
    const select = container.querySelector("select") as HTMLSelectElement;
    expect(select.querySelectorAll("option")).toHaveLength(2);
    const textInput = container.querySelector("input[type=text]") as HTMLInputElement;
    expect(textInput.value).toBe("");
  });

  it("renders the owner label interpolated with the extension name", () => {
    const { container } = render(
      <ExtensionAppSettingsSection
        section={makeSection([
          makeItem({ extension_name: "My Ext", label: "x" }),
        ])}
        onSaved={onSavedSpy()}
      />,
    );
    const owner = container.querySelector(".extension-app-settings-owner");
    expect(owner?.textContent).toBe("settings.extensionAppSettingsOwner:My Ext");
  });
});
