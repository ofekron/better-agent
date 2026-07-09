import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string) => ({
      "extensions.catalogUnavailable": "UI extensions could not be loaded.",
      "extensions.resetSettings": "Reset extension settings",
      "extensions.resettingSettings": "Resetting…",
      "extensions.resetSettingsWarning": "This deletes non-secret extension settings and user instructions.",
      "extensions.resetSettingsFailed": "Settings could not be reset. Reload and try again.",
    })[key] ?? key,
  }),
}));

import {
  ExtensionCatalogRecovery,
  useExtensionFrontendCatalog,
} from "../src/components/ExtensionSlots";
import { eventBus } from "../src/lib/eventBus";

function CatalogProbe() {
  const catalog = useExtensionFrontendCatalog("input-overflow-menu");
  return (
    <>
      <ExtensionCatalogRecovery catalog={catalog} />
      {catalog.modules.map((module) => <span key={module.id}>{module.id}</span>)}
    </>
  );
}

describe("extension catalog recovery", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("surfaces incompatible settings and restores Supervisor after explicit reset", async () => {
    let catalogRequests = 0;
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "POST") {
        return new Response(JSON.stringify({ schema_version: 2 }), { status: 200 });
      }
      catalogRequests += 1;
      if (catalogRequests === 1) {
        return new Response(JSON.stringify({
          detail: {
            error: "extension_settings_incompatible",
            reset_available: true,
            found_schema: 1,
            revision: "a".repeat(64),
          },
        }), { status: 409 });
      }
      return new Response(JSON.stringify({
        entrypoints: [{
          extension_id: "ofek-dev.supervisor",
          name: "Supervisor",
          frontend_modules: [
            {
              slot: "input-overflow-menu",
              id: "supervisor-controls",
              label: "Supervisor",
              kind: "module",
              module_url: "/api/extensions/ofek-dev.supervisor/frontend/ui/supervisor-controls.entry.js",
            },
            {
              slot: "chat-inline-actions",
              id: "supervisor-verdict",
              label: "Supervisor verdict",
              kind: "module",
              module_url: "/api/extensions/ofek-dev.supervisor/frontend/ui/supervisor-verdict.entry.js",
            },
          ],
        }],
      }), { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<CatalogProbe />);

    expect((await screen.findByRole("alert")).textContent).toContain("UI extensions could not be loaded.");
    expect(screen.getByText("This deletes non-secret extension settings and user instructions.")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Reset extension settings" }));

    expect(await screen.findByText("supervisor-controls")).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/api/extensions/settings/reset"),
      expect.objectContaining({ method: "POST", credentials: "include" }),
    );
    const resetCall = fetchMock.mock.calls.find(([, init]) => init?.method === "POST");
    expect(JSON.parse(String(resetCall?.[1]?.body))).toEqual({
      expected_found_schema: 1,
      expected_revision: "a".repeat(64),
    });
  });

  it("refetches the catalog after an extensions_changed recovery event", async () => {
    let requests = 0;
    vi.stubGlobal("fetch", vi.fn(async () => {
      requests += 1;
      if (requests === 1) {
        return new Response(JSON.stringify({ detail: { error: "extension_catalog_unavailable" } }), {
          status: 503,
        });
      }
      return new Response(JSON.stringify({
        entrypoints: [{
          extension_id: "ofek-dev.supervisor",
          name: "Supervisor",
          frontend_modules: [{
            slot: "input-overflow-menu",
            id: "supervisor-controls",
            label: "Supervisor",
            kind: "module",
            module_url: "/api/extensions/ofek-dev.supervisor/frontend/ui/supervisor-controls.entry.js",
          }],
        }],
      }), { status: 200 });
    }));

    render(<CatalogProbe />);
    expect(await screen.findByRole("alert")).toBeTruthy();

    eventBus.publish("extensions_changed", {});

    expect(await screen.findByText("supervisor-controls")).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
