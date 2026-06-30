import { render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import "../src/i18n";
import { ProjectTabs } from "../src/components/ProjectTabs";
import { SessionTabs } from "../src/components/SessionTabs";
import type { Project } from "../src/types";
import { makeSession } from "./fixtures";

function installTabGeometry(
  containerSelector: string,
  activeSelector: string,
): ReturnType<typeof vi.fn> {
  const scrollTo = vi.fn();
  const container = document.querySelector<HTMLElement>(containerSelector);
  const active = document.querySelector<HTMLElement>(activeSelector);
  if (!container || !active) throw new Error("tab geometry target missing");

  Object.defineProperty(container, "clientWidth", {
    value: 300,
    configurable: true,
  });
  Object.defineProperty(container, "scrollWidth", {
    value: 1000,
    configurable: true,
  });
  Object.defineProperty(container, "scrollLeft", {
    value: 0,
    configurable: true,
  });
  container.getBoundingClientRect = () => ({
    left: 0,
    right: 300,
    top: 0,
    bottom: 40,
    width: 300,
    height: 40,
    x: 0,
    y: 0,
    toJSON: () => ({}),
  });
  active.getBoundingClientRect = () => ({
    left: 420,
    right: 540,
    top: 0,
    bottom: 40,
    width: 120,
    height: 40,
    x: 420,
    y: 0,
    toJSON: () => ({}),
  });
  container.scrollTo = scrollTo;
  active.scrollIntoView = vi.fn();
  return scrollTo;
}

describe("top tab auto-scroll", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("centers the active session tab wrapper when the selected session changes", async () => {
    vi.spyOn(window, "matchMedia").mockReturnValue({
      matches: true,
    } as MediaQueryList);
    const sessions = [
      makeSession({ id: "sess-1", name: "One", cwd: "/tmp/project-a" }),
      makeSession({ id: "sess-2", name: "Two", cwd: "/tmp/project-a" }),
    ];
    const { rerender } = render(
      <SessionTabs
        sessions={sessions}
        providers={[]}
        currentSessionId="sess-1"
        sortField="updated_at"
        onSelect={vi.fn()}
        onClose={vi.fn()}
        onCloseOthers={vi.fn()}
        onToggleTopbarPin={vi.fn()}
      />,
    );

    const scrollTo = installTabGeometry(
      ".session-tabs",
      '[data-tab-movement-key="sess-2"]',
    );
    rerender(
      <SessionTabs
        sessions={sessions}
        providers={[]}
        currentSessionId="sess-2"
        sortField="updated_at"
        onSelect={vi.fn()}
        onClose={vi.fn()}
        onCloseOthers={vi.fn()}
        onToggleTopbarPin={vi.fn()}
      />,
    );

    await waitFor(() => {
      expect(scrollTo).toHaveBeenCalledWith({ left: 330, behavior: "auto" });
    });
    expect(
      (document.querySelector('[data-tab-movement-key="sess-2"]') as HTMLElement)
        .scrollIntoView,
    ).not.toHaveBeenCalled();
  });

  it("does not scroll session tabs when only the tab list changes", async () => {
    vi.spyOn(window, "matchMedia").mockReturnValue({
      matches: true,
    } as MediaQueryList);
    const sessions = [
      makeSession({ id: "sess-1", name: "One", cwd: "/tmp/project-a" }),
      makeSession({ id: "sess-2", name: "Two", cwd: "/tmp/project-a" }),
    ];
    const { rerender } = render(
      <SessionTabs
        sessions={sessions}
        providers={[]}
        currentSessionId="sess-2"
        sortField="updated_at"
        onSelect={vi.fn()}
        onClose={vi.fn()}
        onCloseOthers={vi.fn()}
        onToggleTopbarPin={vi.fn()}
      />,
    );

    const scrollTo = installTabGeometry(
      ".session-tabs",
      '[data-tab-movement-key="sess-2"]',
    );
    rerender(
      <SessionTabs
        sessions={[
          makeSession({ id: "sess-3", name: "Three", cwd: "/tmp/project-a" }),
          ...sessions,
        ]}
        providers={[]}
        currentSessionId="sess-2"
        sortField="updated_at"
        onSelect={vi.fn()}
        onClose={vi.fn()}
        onCloseOthers={vi.fn()}
        onToggleTopbarPin={vi.fn()}
      />,
    );

    expect(scrollTo).not.toHaveBeenCalled();
  });

  it("scrolls the active project tab wrapper through the tabs container", async () => {
    vi.spyOn(window, "matchMedia").mockReturnValue({
      matches: true,
    } as MediaQueryList);
    const projects: Project[] = [
      {
        path: "/tmp/project-a",
        node_id: "primary",
        name: "A",
        created_at: "2026-01-01T00:00:00.000Z",
        last_used: "2026-01-01T00:00:00.000Z",
      },
      {
        path: "/tmp/project-b",
        node_id: "primary",
        name: "B",
        created_at: "2026-01-01T00:00:00.000Z",
        last_used: "2026-01-01T00:00:00.000Z",
      },
    ];
    const { rerender } = render(
      <ProjectTabs
        projects={projects}
        currentPath="/tmp/project-a"
        currentNodeId="primary"
        onSelect={vi.fn()}
        onAdd={vi.fn()}
        onRemove={vi.fn()}
        onOpenSettings={vi.fn()}
      />,
    );

    const scrollTo = installTabGeometry(
      ".project-tabs",
      '[data-tab-movement-key="primary::/tmp/project-b"]',
    );
    rerender(
      <ProjectTabs
        projects={projects}
        currentPath="/tmp/project-b"
        currentNodeId="primary"
        onSelect={vi.fn()}
        onAdd={vi.fn()}
        onRemove={vi.fn()}
        onOpenSettings={vi.fn()}
      />,
    );

    await waitFor(() => {
      expect(scrollTo).toHaveBeenCalledWith({ left: 240, behavior: "auto" });
    });
    expect(
      (document.querySelector('[data-tab-movement-key="primary::/tmp/project-b"]') as HTMLElement)
        .scrollIntoView,
    ).not.toHaveBeenCalled();
  });
});
