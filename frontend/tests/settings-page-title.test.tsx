import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import "../src/i18n";
import { SettingsPage } from "../src/components/SettingsPage";

function jsonResponse(body: unknown) {
  return Promise.resolve({
    ok: true,
    json: () => Promise.resolve(body),
  } as Response);
}

describe("SettingsPage title", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("labels the page as Settings, not Setup", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = String(input);
      if (url.includes("/api/providers")) {
        return jsonResponse({ providers: [], default_provider_id: null });
      }
      if (url.includes("/api/provider-setup/status")) {
        return jsonResponse({ providers: [] });
      }
      if (url.includes("/api/user-prefs")) {
        return jsonResponse({ first_run_wizard_done: true, network_bind_address: "127.0.0.1" });
      }
      if (url.includes("/api/projects")) {
        return jsonResponse({ projects: [] });
      }
      if (url.includes("/api/provider-config-sync/repository")) {
        return jsonResponse({ configured: false });
      }
      if (url.includes("/api/settings/password-manager")) {
        return jsonResponse({ items: [] });
      }
      return Promise.resolve({ ok: false, status: 404, text: () => Promise.resolve("") } as Response);
    });

    render(
      <SettingsPage
        onClose={() => {}}
        onOpenProviderConfigSync={() => {}}
      />,
    );

    expect(await screen.findByRole("heading", { name: "Settings" })).toBeTruthy();
    await waitFor(() => {
      expect(screen.queryByRole("heading", { name: "Setup" })).toBeNull();
    });
  });

  it("marks the selected settings section in the navigation", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = String(input);
      if (url.includes("/api/providers")) {
        return jsonResponse({ providers: [], default_provider_id: null });
      }
      if (url.includes("/api/provider-setup/status")) {
        return jsonResponse({ providers: [] });
      }
      if (url.includes("/api/user-prefs")) {
        return jsonResponse({ first_run_wizard_done: true, network_bind_address: "127.0.0.1" });
      }
      if (url.includes("/api/projects")) {
        return jsonResponse({ projects: [] });
      }
      if (url.includes("/api/provider-config-sync/repository")) {
        return jsonResponse({ configured: false });
      }
      if (url.includes("/api/settings/password-manager")) {
        return jsonResponse({ items: [] });
      }
      return Promise.resolve({ ok: false, status: 404, text: () => Promise.resolve("") } as Response);
    });

    render(
      <SettingsPage
        onClose={() => {}}
        onOpenProviderConfigSync={() => {}}
      />,
    );

    const providersTab = await screen.findByRole("button", { name: "Providers" });
    expect(providersTab.getAttribute("aria-current")).toBe("page");

    fireEvent.click(screen.getByRole("button", { name: "Appearance" }));

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Appearance" }).getAttribute("aria-current")).toBe("page");
      expect(providersTab.getAttribute("aria-current")).toBeNull();
    });
  });

  it("does not require a finish action in the first-run wizard", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = String(input);
      if (url.includes("/api/providers")) {
        return jsonResponse({ providers: [], default_provider_id: null });
      }
      if (url.includes("/api/provider-setup/status")) {
        return jsonResponse({ providers: [] });
      }
      if (url.includes("/api/user-prefs")) {
        return jsonResponse({ first_run_wizard_done: false, network_bind_address: "127.0.0.1" });
      }
      if (url.includes("/api/projects")) {
        return jsonResponse({ projects: [] });
      }
      if (url.includes("/api/provider-config-sync/repository")) {
        return jsonResponse({ enabled: false, remote_url: "" });
      }
      if (url.includes("/api/settings/password-manager")) {
        return jsonResponse({ items: [] });
      }
      return Promise.resolve({ ok: false, status: 404, text: () => Promise.resolve("") } as Response);
    });

    render(
      <SettingsPage
        onClose={() => {}}
        onOpenProviderConfigSync={() => {}}
      />,
    );

    await screen.findByRole("heading", { name: "Set up Better Agent" });
    expect(screen.queryByRole("button", { name: "Finish setup" })).toBeNull();
  });

  it("explains and persists the first-run network bind choice", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = String(input);
      if (url.includes("/api/providers")) {
        return jsonResponse({ providers: [], default_provider_id: null });
      }
      if (url.includes("/api/provider-setup/status")) {
        return jsonResponse({ providers: [] });
      }
      if (url.includes("/api/user-prefs") && init?.method === "PATCH") {
        return jsonResponse({ first_run_wizard_done: false, network_bind_address: "0.0.0.0" });
      }
      if (url.includes("/api/user-prefs")) {
        return jsonResponse({ first_run_wizard_done: false, network_bind_address: "127.0.0.1" });
      }
      if (url.includes("/api/projects")) {
        return jsonResponse({ projects: [] });
      }
      if (url.includes("/api/provider-config-sync/repository")) {
        return jsonResponse({ enabled: false, remote_url: "" });
      }
      if (url.includes("/api/settings/password-manager")) {
        return jsonResponse({ items: [] });
      }
      return Promise.resolve({ ok: false, status: 404, text: () => Promise.resolve("") } as Response);
    });

    render(
      <SettingsPage
        onClose={() => {}}
        onRefreshApp={() => {}}
        onOpenProviderConfigSync={() => {}}
      />,
    );

    await screen.findByRole("heading", { name: "Set up Better Agent" });
    expect(screen.getByText("Network access")).toBeTruthy();
    expect(screen.getByText("Local-only is safer. Network access can expose the app to other devices on reachable networks, so use it only on trusted networks with firewall rules you understand.")).toBeTruthy();

    fireEvent.click(screen.getByLabelText("Network devices"));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/user-prefs",
        expect.objectContaining({
          method: "PATCH",
          body: JSON.stringify({ network_bind_address: "0.0.0.0" }),
        }),
      );
    });
  });

  it("groups extension controls with plain-language labels", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = String(input);
      if (url.includes("/api/providers")) {
        return jsonResponse({ providers: [], default_provider_id: null });
      }
      if (url.includes("/api/provider-setup/status")) {
        return jsonResponse({ providers: [] });
      }
      if (url.includes("/api/user-prefs")) {
        return jsonResponse({ first_run_wizard_done: true, network_bind_address: "127.0.0.1" });
      }
      if (url.includes("/api/projects")) {
        return jsonResponse({ projects: [] });
      }
      if (url.includes("/api/provider-config-sync/repository")) {
        return jsonResponse({ configured: false });
      }
      if (url.includes("/api/settings/password-manager")) {
        return jsonResponse({ items: [] });
      }
      if (url.endsWith("/api/extensions")) {
        return jsonResponse({
          extensions: [
            {
              enabled: true,
              manifest: {
                id: "ofek.scheduler",
                entrypoints: {
                  instructions: [{ name: "scheduler", level: "global" }],
                },
              },
              instructions_enabled: { global: true, projects: {} },
            },
          ],
        });
      }
      if (url.includes("/api/extensions/ofek.scheduler/config")) {
        return jsonResponse({
          name: "Scheduler",
          has_quick_button: true,
          has_page: false,
          ui: { quick_button_enabled: true },
          mcp: [{ name: "scheduler", label: "scheduler", enabled: true }],
          settings: { schema: [], values: {}, secret_present: {} },
          permissions: {
            declared: {
              session_state: true,
              internal_loopback: true,
              filesystem: "optional",
              mutates_session_fields: ["rearranger_enabled"],
            },
            optional: ["filesystem"],
            grants: {},
          },
        });
      }
      return Promise.resolve({ ok: false, status: 404, text: () => Promise.resolve("") } as Response);
    });

    render(
      <SettingsPage
        onClose={() => {}}
        onOpenProviderConfigSync={() => {}}
      />,
    );

    fireEvent.click(await screen.findByRole("button", { name: "Extensions" }));

    expect(await screen.findByText("Scheduler")).toBeTruthy();
    const row = screen.getByText("Scheduler").closest(".extension-ui-settings-row");
    const groups = row?.querySelector(".extension-ui-settings-groups");
    expect(groups).toBeTruthy();
    expect(groups?.querySelectorAll(".extension-ui-settings-group")).toHaveLength(4);
    expect(screen.getByText("App UI")).toBeTruthy();
    expect(screen.getByText("Buttons or pages this extension adds to Better Agent.")).toBeTruthy();
    expect(screen.getByText("Agent tools")).toBeTruthy();
    expect(screen.getByText("MCP servers exposed as tools to Claude, Codex, or Gemini runs.")).toBeTruthy();
    expect(screen.getByText("Permissions")).toBeTruthy();
    expect(screen.getByText("Read and update sessions")).toBeTruthy();
    expect(screen.getByText(/buggy or malicious extension could expose or alter your conversations/)).toBeTruthy();
    expect(screen.getByText("Call Better Agent internals")).toBeTruthy();
    expect(screen.getByText("Access files")).toBeTruthy();
    expect(screen.getByLabelText("Blocked")).toBeTruthy();
    expect(screen.getByText("Change selected session fields")).toBeTruthy();
    expect(screen.getByText("Limited to: rearranger_enabled")).toBeTruthy();
  });

  it("shows desktop app downloads in settings", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = String(input);
      if (url.includes("/api/providers")) {
        return jsonResponse({ providers: [], default_provider_id: null });
      }
      if (url.includes("/api/provider-setup/status")) {
        return jsonResponse({ providers: [] });
      }
      if (url.includes("/api/user-prefs")) {
        return jsonResponse({ first_run_wizard_done: true, network_bind_address: "127.0.0.1" });
      }
      if (url.includes("/api/projects")) {
        return jsonResponse({ projects: [] });
      }
      if (url.includes("/api/provider-config-sync/repository")) {
        return jsonResponse({ configured: false });
      }
      if (url.includes("/api/settings/password-manager")) {
        return jsonResponse({ items: [] });
      }
      if (url.includes("/api/desktop/status")) {
        return jsonResponse({ macos: true, windows: false, version: "0.1.42", desktop_shell: false });
      }
      return Promise.resolve({ ok: false, status: 404, text: () => Promise.resolve("") } as Response);
    });

    render(
      <SettingsPage
        onClose={() => {}}
        onOpenProviderConfigSync={() => {}}
      />,
    );

    fireEvent.click(await screen.findByRole("button", { name: "Desktop app" }));

    const macDownload = await screen.findByRole("link", { name: /Download for macOS Available/ });
    expect(macDownload.getAttribute("href")).toBe("/api/download/desktop/macos");
    expect(screen.getByText("0.1.42")).toBeTruthy();
    expect(screen.getByText("Download for Windows")).toBeTruthy();
    expect(screen.getByText("Not built on this server")).toBeTruthy();
  });
});
