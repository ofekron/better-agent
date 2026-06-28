import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ExtensionQuickButtons, runHookAction } from "../src/components/ExtensionUiHooks";

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
                extension_id: "test.assistant",
                extension_name: "Assistant",
                label: "Assistant",
                icon: "sparkles",
                action: {
                  type: "ensure",
                  endpoint: "/api/assistant/ensure",
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
    if (url.endsWith("/api/assistant/ensure") && init?.method === "POST") {
      return {
        ok: true,
        json: async () => ({ id: "assistant-session" }),
      } as Response;
    }
    throw new Error(`unexpected fetch ${url}`);
  });
  return fetchMock;
}

describe("ExtensionQuickButtons", () => {
  it("renders generic Assistant quick button and navigates through ensure action", async () => {
    const fetchMock = mockHooksFetch();
    const navigate = vi.fn();
    render(<ExtensionQuickButtons context={{ navigate, cwd: "/repo" }} variant="toolbar" />);

    const button = await screen.findByRole("button", { name: "Assistant" });
    fireEvent.click(button);

    await waitFor(() => expect(navigate).toHaveBeenCalledWith("/s/assistant-session"));
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringMatching(/\/api\/assistant\/ensure$/),
      expect.objectContaining({
        method: "POST",
        body: "{}",
      }),
    );
  });

  it("renders the same hook in mobile topbar variant", async () => {
    mockHooksFetch();
    render(<ExtensionQuickButtons context={{ navigate: vi.fn(), cwd: "" }} variant="topbar" />);

    expect(await screen.findByRole("button", { name: "Assistant" })).toBeTruthy();
  });

  it("marks ensured session ids before navigating", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true,
      json: async () => ({ id: "assistant-session" }),
    } as Response);
    const navigate = vi.fn();
    const markSessionKnown = vi.fn();

    await runHookAction(
      {
        type: "ensure",
        endpoint: "/api/extensions/test.assistant/backend/assistant/ensure",
        path_template: "/s/{id}",
        id_field: "id",
      },
      { navigate, cwd: "", markSessionKnown },
    );

    expect(markSessionKnown).toHaveBeenCalledWith("assistant-session");
    expect(navigate).toHaveBeenCalledWith("/s/assistant-session");
  });

  it("upgrades stale virtual Assistant navigations through the ensure endpoint", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true,
      json: async () => ({ id: "assistant-session" }),
    } as Response);
    const navigate = vi.fn();
    const markSessionKnown = vi.fn();

    await runHookAction(
      { type: "navigate", path: "/s/virtual:test.assistant:assistant" },
      { navigate, cwd: "", markSessionKnown },
    );

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringMatching(/\/api\/extensions\/test\.assistant\/backend\/assistant\/ensure$/),
      expect.objectContaining({ method: "POST" }),
    );
    expect(markSessionKnown).toHaveBeenCalledWith("assistant-session");
    expect(navigate).toHaveBeenCalledWith("/s/assistant-session");
  });
});
