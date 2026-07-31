import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import "../src/i18n";
import { ExtensionUiSettingsSection } from "../src/components/SettingsPage";

function jsonResponse(body: unknown, ok = true) {
  return Promise.resolve({
    ok,
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  } as Response);
}

interface FakeExtension {
  id: string;
  name: string;
  enabled: boolean;
  required?: boolean;
  description?: string;
}

/** Routes the fetch calls ExtensionUiSettingsSection makes against a mutable
 * extension list, so post-mutation refreshes observe backend state changes. */
function routeExtensionsFetch(
  extensions: FakeExtension[],
  overrides?: (url: string, init?: RequestInit) => Promise<Response> | null,
) {
  return vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
    const url = String(input);
    const override = overrides?.(url, init);
    if (override) return override;
    if (url.endsWith("/api/extensions?include_hidden=true") && !init?.method) {
      return jsonResponse({
        extensions: extensions.map((e) => ({
          enabled: e.enabled,
          manifest: { id: e.id, description: e.description, entrypoints: {} },
        })),
      });
    }
    if (url.endsWith("/api/extensions/updates") && !init?.method) {
      return jsonResponse({ results: [] });
    }
    const config = extensions.find((e) => url.endsWith(`/api/extensions/${e.id}/config`));
    if (config && !init?.method) {
      return jsonResponse({ name: config.name, required: config.required === true });
    }
    throw new Error(`unexpected fetch ${url}`);
  });
}

describe("ExtensionUiSettingsSection uninstall", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("lists installed extensions and uninstalls only after confirm", async () => {
    const extensions: FakeExtension[] = [
      { id: "ofek.empty-extension", name: "Empty Extension", enabled: true, description: "Does nothing" },
    ];
    const fetchMock = routeExtensionsFetch(extensions, (url, init) => {
      if (url.endsWith("/api/extensions/ofek.empty-extension") && init?.method === "DELETE") {
        extensions.length = 0;
        return jsonResponse({ ok: true });
      }
      return null;
    });
    // happy-dom (this suite's test environment) does not implement
    // window.confirm, so it can't be vi.spyOn'd (no function to wrap) —
    // install a plain replacement instead, same as forkSplit.test.tsx.
    const originalConfirm = window.confirm;
    const confirmMock = vi.fn().mockReturnValue(false);
    window.confirm = confirmMock;

    try {
      render(<ExtensionUiSettingsSection />);

      expect(await screen.findByText("Empty Extension")).toBeTruthy();
      expect(screen.getByText("ofek.empty-extension")).toBeTruthy();
      expect(screen.getByText("Does nothing")).toBeTruthy();

      fireEvent.click(screen.getByRole("button", { name: /Uninstall/ }));
      expect(confirmMock).toHaveBeenCalledWith("Uninstall Empty Extension?");
      expect(fetchMock).not.toHaveBeenCalledWith(
        expect.stringMatching(/\/api\/extensions\/ofek\.empty-extension$/),
        expect.objectContaining({ method: "DELETE" }),
      );

      confirmMock.mockReturnValue(true);
      fireEvent.click(screen.getByRole("button", { name: /Uninstall/ }));

      await waitFor(() => {
        expect(fetchMock).toHaveBeenCalledWith(
          expect.stringMatching(/\/api\/extensions\/ofek\.empty-extension$/),
          expect.objectContaining({ method: "DELETE" }),
        );
      });
      await waitFor(() => expect(screen.queryByText("Empty Extension")).toBeNull());
      expect(await screen.findByText("No extensions installed.")).toBeTruthy();
    } finally {
      window.confirm = originalConfirm;
    }
  });

  it("blocks disabling and uninstalling required extensions", async () => {
    routeExtensionsFetch([
      { id: "ofek-dev.marketplace", name: "Marketplace", enabled: true, required: true },
    ]);

    render(<ExtensionUiSettingsSection />);

    expect(await screen.findByText("Marketplace")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Uninstall/ })).toBeNull();
    const toggle = screen.getByRole("checkbox", { name: /Enabled/ }) as HTMLInputElement;
    expect(toggle.checked).toBe(true);
    expect(toggle.disabled).toBe(true);
  });

  it("re-enables a disabled extension via PATCH", async () => {
    const fetchMock = routeExtensionsFetch(
      [{ id: "ofek.disabled-extension", name: "Disabled Extension", enabled: false }],
      (url, init) => {
        if (url.endsWith("/api/extensions/ofek.disabled-extension/enabled") && init?.method === "PATCH") {
          return jsonResponse({ extension: { enabled: true } });
        }
        return null;
      },
    );

    render(<ExtensionUiSettingsSection />);

    expect(await screen.findByText("Disabled Extension")).toBeTruthy();
    const toggle = screen.getByRole("checkbox", { name: /Disabled/ }) as HTMLInputElement;
    expect(toggle.checked).toBe(false);
    fireEvent.click(toggle);

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringMatching(/\/api\/extensions\/ofek\.disabled-extension\/enabled$/),
        expect.objectContaining({
          method: "PATCH",
          body: JSON.stringify({ enabled: true }),
        }),
      );
    });
    expect(await screen.findByRole("checkbox", { name: /Enabled/ })).toBeTruthy();
  });

  it("filters installed extensions by search text", async () => {
    routeExtensionsFetch([
      { id: "ofek.alpha-extension", name: "Alpha Extension", enabled: true },
      { id: "ofek.beta-extension", name: "Beta Extension", enabled: true },
    ]);

    render(<ExtensionUiSettingsSection />);

    expect(await screen.findByText("Alpha Extension")).toBeTruthy();
    expect(screen.getByText("Beta Extension")).toBeTruthy();

    const searchBox = screen.getByRole("textbox", { name: "Search extensions…" });
    fireEvent.change(searchBox, { target: { value: "beta" } });

    expect(screen.queryByText("Alpha Extension")).toBeNull();
    expect(screen.getByText("Beta Extension")).toBeTruthy();

    fireEvent.change(searchBox, { target: { value: "missing" } });

    expect(screen.queryByText("Beta Extension")).toBeNull();
    expect(screen.getByText("No extensions match your search.")).toBeTruthy();
  });

  it("scopes enable toggles and uninstall buttons per row", async () => {
    routeExtensionsFetch([
      { id: "ofek.alpha-extension", name: "Alpha Extension", enabled: true },
      { id: "ofek-dev.marketplace", name: "Marketplace", enabled: true, required: true },
    ]);

    render(<ExtensionUiSettingsSection />);

    expect(await screen.findByText("Alpha Extension")).toBeTruthy();
    const alphaRow = screen.getByText("Alpha Extension").closest(".extension-ui-settings-row") as HTMLElement;
    const marketplaceRow = screen.getByText("Marketplace").closest(".extension-ui-settings-row") as HTMLElement;

    expect(within(alphaRow).getByRole("button", { name: /Uninstall/ })).toBeTruthy();
    expect((within(alphaRow).getByRole("checkbox") as HTMLInputElement).disabled).toBe(false);
    expect(within(marketplaceRow).queryByRole("button", { name: /Uninstall/ })).toBeNull();
    expect((within(marketplaceRow).getByRole("checkbox") as HTMLInputElement).disabled).toBe(true);
  });
});
