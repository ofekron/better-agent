import { describe, expect, it, vi } from "vitest";
import { act } from "react";
import { renderApp } from "./harness";
import { makeSession } from "./fixtures";

describe("settings navigation", () => {
  it("opens Settings through the app route so back returns to chat", async () => {
    window.history.pushState(null, "", "/");
    const openSpy = vi.spyOn(window, "open").mockReturnValue(null);
    const session = makeSession({ id: "settings-back", name: "Settings Back" });
    const h = await renderApp({ seed: { sessions: [session] } });

    await h.selectSession(session.id);
    expect(window.location.pathname).toBe("/s/settings-back");

    await h.click('button[aria-label="app.settingsButtonTitle"]');
    expect(openSpy).not.toHaveBeenCalled();
    expect(window.location.pathname).toBe("/settings");
    expect(h.$(".settings-page")).not.toBeNull();

    await act(async () => {
      window.history.back();
    });
    await h.flush();

    expect(window.location.pathname).toBe("/s/settings-back");
    expect(h.$(".settings-page")).toBeNull();
    expect(h.$('[data-testid="chat-messages"]')).not.toBeNull();

    openSpy.mockRestore();
    h.unmount();
  });
});
