import { afterEach, beforeEach, describe, expect, it } from "vitest";
import "../src/i18n";
import { renderApp } from "./harness";
import { makeSession } from "./fixtures";

describe("per-session right panel state", () => {
  const defaultViewport = {
    width: window.innerWidth,
    height: window.innerHeight,
  };

  function setViewport(width: number, height: number): void {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: width });
    Object.defineProperty(window, "innerHeight", { configurable: true, value: height });
    window.dispatchEvent(new Event("resize"));
  }

  beforeEach(() => {
    setViewport(1280, 900);
  });

  afterEach(() => {
    setViewport(defaultViewport.width, defaultViewport.height);
  });

  it("restores right panel tab, size, and sidebar collapse from the selected session", async () => {
    const session = makeSession({
      id: "panel",
      name: "Panel session",
      right_panel_open: true,
      right_panel_active_tab: "notes",
      right_panel_width: 520,
      sidebar_minimized: true,
    });
    const h = await renderApp({ seed: { sessions: [session] } });

    await h.flush();

    expect(h.$(".right-panel:not(.right-panel-collapsed)")).toBeTruthy();
    expect(h.$(".right-panel-tab.active")?.textContent).toContain("Notes");
    expect((h.$(".right-panel") as HTMLElement | null)?.style.width).toBe("520px");
    expect(h.$(".sidebar.sidebar-minimized")).toBeTruthy();

    h.unmount();
  });

  it("writes right panel and sidebar mutations back to the session endpoint", async () => {
    const session = makeSession({
      id: "update",
      name: "Update session",
      right_panel_open: false,
      right_panel_active_tab: null,
      sidebar_minimized: false,
    });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    await h.click(".chat-toolbar-right-panel-toggle");
    await h.clickByText("Notes");
    await h.click('[aria-label="Minimize sidebar"]');
    await h.flush();

    const panelCalls = h.restCalls.filter(
      (call) => call.method === "PATCH" && call.path === `/api/sessions/${session.id}/right-panel`,
    );
    expect(panelCalls.some((call) => (call.body as { open?: boolean }).open === true)).toBe(true);
    expect(panelCalls.some((call) => (call.body as { tab?: string }).tab === "notes")).toBe(true);
    expect(panelCalls.some((call) => (call.body as { sidebar_minimized?: boolean }).sidebar_minimized === true)).toBe(true);
    expect(h.backend.state.sessions[0].right_panel_open).toBe(true);
    expect(h.backend.state.sessions[0].right_panel_active_tab).toBe("notes");
    expect(h.backend.state.sessions[0].sidebar_minimized).toBe(true);

    h.unmount();
  });
});
