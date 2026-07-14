import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ExtensionQuickButtons, runHookAction, useExtensionPageBadges } from "../src/components/ExtensionUiHooks";
import { eventBus } from "../src/lib/eventBus";
import { dismissSyncFailure, useSyncStatus } from "../src/progress/store";

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
                icon: "assistant-start",
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
    expect(button.className).toContain("extension-quick-button--icon-assistant-start");
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

  it("reports ensure failures through the generic three-state sync control", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("failed", { status: 500 }));
    const navigate = vi.fn();

    await runHookAction(
      { type: "ensure", endpoint: "/api/assistant/ensure", path_template: "/s/{id}", id_field: "id" },
      { navigate, cwd: "" },
      "Assistant",
    );

    function Probe() {
      const status = useSyncStatus();
      return <span>{status.failures[0]?.action}</span>;
    }
    render(<Probe />);
    expect(screen.getByText("Assistant")).toBeTruthy();
    expect(navigate).not.toHaveBeenCalled();
    act(() => dismissSyncFailure("extensions:hook:/api/assistant/ensure"));
  });
});

describe("useExtensionPageBadges", () => {
  it("loads once and refreshes from project update events without hot polling", async () => {
    const intervalSpy = vi.spyOn(window, "setInterval").mockImplementation(() => 1);
    vi.spyOn(window, "clearInterval").mockImplementation(() => undefined);
    let count = 1;
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith("/api/extensions/ofek.project/backend/project-updates/total")) {
        return {
          ok: true,
          json: async () => ({ count: count++ }),
        } as Response;
      }
      throw new Error(`unexpected fetch ${url}`);
    });

    function Probe() {
      const badges = useExtensionPageBadges([
        {
          extension_id: "ofek.project",
          extension_name: "Project",
          id: "updates",
          label: "Updates",
          icon: "folder",
          open: { type: "navigate", path: "/projects" },
          badge: { endpoint: "/api/extensions/ofek.project/backend/project-updates/total" },
        },
      ]);
      return <div data-testid="badge">{badges["ofek.project:updates"] ?? 0}</div>;
    }

    const view = render(<Probe />);
    await waitFor(() => expect(screen.getByTestId("badge").textContent).toBe("1"));

    expect(intervalSpy).toHaveBeenCalledWith(expect.any(Function), 120_000);
    const initialText = screen.getByTestId("badge").textContent;
    fetchMock.mockClear();

    act(() => {
      eventBus.publish("project_updates_changed", {
        project_id: "repo",
        unseen_count: 2,
      });
    });
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByTestId("badge").textContent).not.toBe(initialText));
    view.unmount();
  });
});
