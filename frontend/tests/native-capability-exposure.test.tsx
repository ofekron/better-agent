import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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

function extensionConfig(nativeExposed = false) {
  return {
    name: "Native Harness",
    required: false,
    has_quick_button: false,
    has_page: false,
    ui: {},
    harness_additions: [
      { kind: "skill", name: "reviewer", native_eligible: true, native_exposed: nativeExposed },
      { kind: "mcp", name: "unsafe-shell", native_eligible: false, native_exposed: false },
    ],
    mcp: [],
    settings: { schema: [], values: {}, secret_present: {} },
    permissions: { declared: {}, optional: [], grants: {} },
  };
}

describe("native capability exposure", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("persists an eligible capability and reflects the backend-confirmed state", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = String(input);
      if (url.endsWith("/api/extensions?include_hidden=true")) {
        return jsonResponse({ extensions: [{ enabled: true, manifest: { id: "ofek.native-harness", entrypoints: {} } }] });
      }
      if (url.endsWith("/api/projects")) return jsonResponse({ projects: [] });
      if (url.endsWith("/api/extensions/ofek.native-harness/config")) return jsonResponse(extensionConfig());
      if (url.endsWith("/harness-additions/skill/reviewer/native-exposure") && init?.method === "PATCH") {
        return jsonResponse({ kind: "skill", name: "reviewer", native_exposed: true });
      }
      throw new Error(`unexpected fetch ${url}`);
    });

    render(<ExtensionUiSettingsSection />);

    const toggle = await screen.findByRole("checkbox", { name: "Expose reviewer to native provider tools" });
    expect((toggle as HTMLInputElement).checked).toBe(false);
    expect(screen.getByText("Not available for native provider tools")).toBeTruthy();

    fireEvent.click(toggle);

    await waitFor(() => expect((toggle as HTMLInputElement).checked).toBe(true));
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringMatching(/\/api\/extensions\/ofek\.native-harness\/harness-additions\/skill\/reviewer\/native-exposure$/),
      expect.objectContaining({ method: "PATCH", body: JSON.stringify({ enabled: true }) }),
    );
  });

  it("keeps backend state and shows the server error when exposure is rejected", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = String(input);
      if (url.endsWith("/api/extensions?include_hidden=true")) {
        return jsonResponse({ extensions: [{ enabled: true, manifest: { id: "ofek.native-harness", entrypoints: {} } }] });
      }
      if (url.endsWith("/api/projects")) return jsonResponse({ projects: [] });
      if (url.endsWith("/api/extensions/ofek.native-harness/config")) return jsonResponse(extensionConfig());
      if (url.endsWith("/harness-additions/skill/reviewer/native-exposure") && init?.method === "PATCH") {
        return jsonResponse({ detail: "Native installation is blocked" }, false);
      }
      throw new Error(`unexpected fetch ${url}`);
    });

    render(<ExtensionUiSettingsSection />);

    const toggle = await screen.findByRole("checkbox", { name: "Expose reviewer to native provider tools" });
    fireEvent.click(toggle);

    expect((await screen.findByRole("alert")).textContent).toBe("Native installation is blocked");
    expect((toggle as HTMLInputElement).checked).toBe(false);
  });
});
