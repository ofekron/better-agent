import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ExtensionQuickButtons } from "../src/components/ExtensionUiHooks";

afterEach(() => {
  vi.restoreAllMocks();
});

function mockHooksFetch() {
  const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = String(input);
    if (url.endsWith("/api/extensions/ui-hooks")) {
      return {
        ok: true,
        json: async () => ({
          hooks: {
            quick_buttons: [
              {
                extension_id: "ofek-dev.ask",
                extension_name: "Ask",
                label: "Ask",
                icon: "sparkles",
                action: {
                  type: "ensure",
                  endpoint: "/api/ask/ensure",
                  path_template: "/s/{id}",
                  id_field: "id",
                },
              },
            ],
            pages: [],
          },
        }),
      } as Response;
    }
    if (url.endsWith("/api/ask/ensure") && init?.method === "POST") {
      return {
        ok: true,
        json: async () => ({ id: "virtual:ofek-dev.ask:ask" }),
      } as Response;
    }
    throw new Error(`unexpected fetch ${url}`);
  });
  return fetchMock;
}

describe("ExtensionQuickButtons", () => {
  it("renders generic Ask quick button and navigates through ensure action", async () => {
    const fetchMock = mockHooksFetch();
    const navigate = vi.fn();
    render(<ExtensionQuickButtons context={{ navigate, cwd: "/repo" }} variant="toolbar" />);

    const button = await screen.findByRole("button", { name: "Ask" });
    fireEvent.click(button);

    await waitFor(() => expect(navigate).toHaveBeenCalledWith("/s/virtual%3Aofek-dev.ask%3Aask"));
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringMatching(/\/api\/ask\/ensure$/),
      expect.objectContaining({
        method: "POST",
        body: "{}",
      }),
    );
  });

  it("renders the same hook in mobile topbar variant", async () => {
    mockHooksFetch();
    render(<ExtensionQuickButtons context={{ navigate: vi.fn(), cwd: "" }} variant="topbar" />);

    expect(await screen.findByRole("button", { name: "Ask" })).toBeTruthy();
  });
});
