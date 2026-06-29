import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import "../src/i18n";
import { ExtensionUiSettingsSection } from "../src/components/SettingsPage";

function jsonResponse(body: unknown) {
  return Promise.resolve({
    ok: true,
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  } as Response);
}

describe("ExtensionUiSettingsSection uninstall", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("shows installed extensions with no configurable surfaces and uninstalls them", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = String(input);
      if (url.endsWith("/api/extensions?include_hidden=true") && !init?.method) {
        return jsonResponse({
          extensions: [
            {
              enabled: true,
              manifest: {
                id: "ofek.empty-extension",
                entrypoints: {},
              },
            },
          ],
        });
      }
      if (url.endsWith("/api/projects")) {
        return jsonResponse({ projects: [] });
      }
      if (url.endsWith("/api/extensions/ofek.empty-extension/config")) {
        return jsonResponse({
          name: "Empty Extension",
          has_quick_button: false,
          has_page: false,
          ui: {},
          mcp: [],
          settings: { schema: [], values: {}, secret_present: {} },
          permissions: { declared: {}, optional: [], grants: {} },
          required: false,
        });
      }
      if (url.endsWith("/api/extensions/ofek.empty-extension") && init?.method === "DELETE") {
        return jsonResponse({ ok: true });
      }
      throw new Error(`unexpected fetch ${url}`);
    });
    vi.spyOn(window, "confirm").mockReturnValue(true);

    render(<ExtensionUiSettingsSection />);

    expect(await screen.findByText("Empty Extension")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Uninstall/ }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringMatching(/\/api\/extensions\/ofek\.empty-extension$/),
        expect.objectContaining({ method: "DELETE" }),
      );
    });
  });

  it("shows hidden required marketplace MCP server and toggles it", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = String(input);
      if (url.endsWith("/api/extensions?include_hidden=true") && !init?.method) {
        return jsonResponse({
          extensions: [
            {
              enabled: true,
              manifest: {
                id: "ofek-dev.marketplace",
                entrypoints: {},
              },
            },
          ],
        });
      }
      if (url.endsWith("/api/projects")) {
        return jsonResponse({ projects: [] });
      }
      if (url.endsWith("/api/extensions/ofek-dev.marketplace/config")) {
        return jsonResponse({
          name: "Marketplace",
          required: true,
          harness_delivery: "runtime",
          has_quick_button: false,
          has_page: false,
          ui: {},
          mcp: [{ name: "ofek-dev-marketplace", label: "ofek-dev-marketplace", enabled: true }],
          settings: { schema: [], values: {}, secret_present: {} },
          permissions: { declared: { internal_loopback: true }, optional: [], grants: {} },
        });
      }
      if (
        url.endsWith("/api/extensions/ofek-dev.marketplace/mcp/ofek-dev-marketplace/enabled") &&
        init?.method === "PATCH"
      ) {
        return jsonResponse({ server: "ofek-dev-marketplace", enabled: false });
      }
      throw new Error(`unexpected fetch ${url}`);
    });

    render(<ExtensionUiSettingsSection />);

    expect(await screen.findByText("Marketplace")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Uninstall/ })).toBeNull();

    fireEvent.click(screen.getByRole("checkbox", { name: /ofek-dev-marketplace/ }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringMatching(/\/api\/extensions\/ofek-dev\.marketplace\/mcp\/ofek-dev-marketplace\/enabled$/),
        expect.objectContaining({
          method: "PATCH",
          body: JSON.stringify({ enabled: false }),
        }),
      );
    });
  });

  it("shows disabled installed extensions and toggles extension enabled state", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = String(input);
      if (url.endsWith("/api/extensions?include_hidden=true") && !init?.method) {
        return jsonResponse({
          extensions: [
            {
              enabled: false,
              manifest: {
                id: "ofek.disabled-extension",
                entrypoints: {},
              },
            },
          ],
        });
      }
      if (url.endsWith("/api/projects")) {
        return jsonResponse({ projects: [] });
      }
      if (url.endsWith("/api/extensions/ofek.disabled-extension/config")) {
        return jsonResponse({
          name: "Disabled Extension",
          required: false,
          harness_delivery: "native",
          has_quick_button: false,
          has_page: false,
          ui: {},
          mcp: [],
          settings: { schema: [], values: {}, secret_present: {} },
          permissions: { declared: {}, optional: [], grants: {} },
        });
      }
      if (url.endsWith("/api/extensions/ofek.disabled-extension/enabled") && init?.method === "PATCH") {
        return jsonResponse({ extension: { enabled: true } });
      }
      throw new Error(`unexpected fetch ${url}`);
    });

    render(<ExtensionUiSettingsSection />);

    expect(await screen.findByText("Disabled Extension")).toBeTruthy();
    fireEvent.click(screen.getByRole("checkbox", { name: /Disabled/ }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringMatching(/\/api\/extensions\/ofek\.disabled-extension\/enabled$/),
        expect.objectContaining({
          method: "PATCH",
          body: JSON.stringify({ enabled: true }),
        }),
      );
    });
  });

  it("shows harness additions on extension items", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = String(input);
      if (url.endsWith("/api/extensions?include_hidden=true") && !init?.method) {
        return jsonResponse({
          extensions: [
            {
              enabled: true,
              manifest: {
                id: "better-agent.harness-for-better-agent",
                entrypoints: {
                  instructions: [{ name: "Better Agent Harness Behavior", level: "global" }],
                  skills: [{ name: "project-structure" }],
                  mcp: [{ name: "better-agent-coordination" }],
                },
              },
            },
          ],
        });
      }
      if (url.endsWith("/api/projects")) {
        return jsonResponse({ projects: [] });
      }
      if (url.endsWith("/api/extensions/better-agent.harness-for-better-agent/config")) {
        return jsonResponse({
          name: "Better Agent Harness",
          required: false,
          harness_delivery: "native",
          has_quick_button: false,
          has_page: false,
          internal_llm_tasks: ["extension_context_audit"],
          ui: {},
          mcp: [],
          settings: { schema: [], values: {}, secret_present: {} },
          permissions: { declared: {}, optional: [], grants: {} },
        });
      }
      if (url.endsWith("/api/settings/internal-llm")) {
        return jsonResponse({ tasks: [], assignments: {} });
      }
      if (url.endsWith("/api/providers")) {
        return jsonResponse({
          default_provider_id: "p1",
          providers: [
            {
              id: "p1",
              name: "Provider",
              default_model: "model-a",
              custom_models: [],
              supports_reasoning_effort: false,
              reasoning_effort_options: [],
            },
          ],
        });
      }
      throw new Error(`unexpected fetch ${url}`);
    });

    render(<ExtensionUiSettingsSection />);

    expect(await screen.findByText("Better Agent Harness")).toBeTruthy();
    expect(screen.getByText("Harness additions")).toBeTruthy();
    expect(screen.getByText("Better Agent Harness Behavior")).toBeTruthy();
    expect(screen.getByText("project-structure")).toBeTruthy();
    expect(screen.getByText("better-agent-coordination")).toBeTruthy();
    expect(await screen.findByText("Extension context audit")).toBeTruthy();
  });
});
