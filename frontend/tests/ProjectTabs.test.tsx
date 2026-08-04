import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, waitFor } from "@testing-library/react";
import React from "react";
import type { Project } from "../src/types";

// --- mocks ------------------------------------------------------------------

vi.mock("src/hooks/useAnimatedTabMovement", () => ({
  useAnimatedTabMovement: () => ({ current: null }),
}));

const scrollSpy = vi.hoisted(() => vi.fn());
vi.mock("src/utils/tabScroll", () => ({
  scrollHorizontalItemIntoView: scrollSpy,
}));

// projectPathName: empty string for "/onlypath" so the `|| p.path` label
// fallback is reachable; everything else is prefixed for determinism.
vi.mock("src/utils/projectPath", () => ({
  projectPathName: (p: string) => (p === "/onlypath" ? "" : "PN:" + p),
}));

vi.mock("../src/components/ProjectStatusBadge", () => ({
  ProjectStatusBadge: () => React.createElement("span", { "data-status-badge": "true" }),
}));

vi.mock("../src/components/Icon", () => ({
  default: ({ name, size }: { name?: string; size?: number }) =>
    React.createElement("span", { "data-icon": name ?? "", "data-size": size ?? "" }),
}));

import { ProjectTabs } from "../src/components/ProjectTabs";

// --- helpers ----------------------------------------------------------------

function project(p: Partial<Project> & { path: string }): Project {
  return { name: "", created_at: "", last_used: "", ...p };
}

function makeProps(overrides: Record<string, unknown> = {}) {
  return {
    projects: [] as Project[],
    currentPath: "",
    currentNodeId: "primary",
    onSelect: vi.fn(),
    onAdd: vi.fn(),
    onRemove: vi.fn(),
    onOpenSettings: vi.fn(),
    projectUpdatesCounts: {},
    disabled: false,
    ...overrides,
  };
}

function dispatchWin(type: string, init: Record<string, unknown> = {}): void {
  act(() => {
    window.dispatchEvent(new Event(type, init));
  });
}

function pressKey(key: string): void {
  act(() => {
    window.dispatchEvent(new KeyboardEvent("keydown", { key }));
  });
}

beforeEach(() => {
  scrollSpy.mockReset();
});

afterEach(() => {
  vi.restoreAllMocks();
});

// --- tests ------------------------------------------------------------------

describe("ProjectTabs", () => {
  it("renders a tab per project, marks the active one, and resolves labels through name→pathName→path", () => {
    const projects = [
      project({ path: "/a/foo", name: "Foo", node_id: "n1" }),
      project({ path: "/b/bar", name: "" }),
    ];
    const { container } = render(
      <ProjectTabs {...makeProps({ projects, currentPath: "/a/foo", currentNodeId: "n1" })} />,
    );

    const tabs = container.querySelectorAll(".project-tab");
    expect(tabs).toHaveLength(2);
    expect(tabs[0]!.className).toContain("active");
    expect(tabs[1]!.className).not.toContain("active");
    expect(tabs[0]!.querySelector(".project-tab-label")!.textContent).toBe("Foo");
    // name empty → projectPathName fallback.
    expect(tabs[1]!.querySelector(".project-tab-label")!.textContent).toBe("PN:/b/bar");
    expect(scrollSpy).toHaveBeenCalled();
  });

  it("falls back to the raw path when name and pathName are both empty", () => {
    const projects = [project({ path: "/onlypath", name: "" })];
    const { container } = render(<ProjectTabs {...makeProps({ projects })} />);
    expect(container.querySelector(".project-tab-label")!.textContent).toBe("/onlypath");
  });

  it("selects a tab via onSelect with the primary node fallback", () => {
    const onSelect = vi.fn();
    const projects = [project({ path: "/x" })]; // node_id undefined → "primary"
    const { container } = render(<ProjectTabs {...makeProps({ projects, onSelect })} />);
    fireEvent.click(container.querySelector(".project-tab-select")!);
    expect(onSelect).toHaveBeenCalledWith("/x", "primary");
  });

  it("disables interaction and shows the AI-search title when disabled", () => {
    const onSelect = vi.fn();
    const onAdd = vi.fn();
    const projects = [project({ path: "/x", node_id: "n1" })];
    const { container } = render(
      <ProjectTabs {...makeProps({ projects, onSelect, onAdd, disabled: true })} />,
    );

    const selectBtn = container.querySelector(".project-tab-select") as HTMLButtonElement;
    expect(selectBtn.disabled).toBe(true);
    expect(selectBtn.title).toContain("AI search bypasses the project filter");
    fireEvent.click(selectBtn);
    expect(onSelect).not.toHaveBeenCalled();

    // Non-disabled title variant on the config button still resolves.
    const { container: c2 } = render(
      <ProjectTabs {...makeProps({ projects, disabled: false })} />,
    );
    expect((c2.querySelector(".project-tab-select") as HTMLButtonElement).title).toBe(
      "n1 — /x",
    );
  });

  it("renders an updates badge when a count is present (slash/underscore encoded), and omits it otherwise", () => {
    const projects = [
      project({ path: "/a/b", name: "P" }), // encoded "-a-b"
      project({ path: "", name: "Root" }), // encoded "" → "root"
      project({ path: "/c", name: "NoBadge" }),
    ];
    const { container } = render(
      <ProjectTabs
        {...makeProps({
          projects,
          projectUpdatesCounts: { "-a-b": 5, root: 3 },
        })}
      />,
    );

    const tabs = container.querySelectorAll(".project-tab");
    expect(tabs[0]!.querySelector(".project-tab-updates-badge")!.textContent).toBe("5");
    expect(tabs[1]!.querySelector(".project-tab-updates-badge")!.textContent).toBe("3");
    expect(tabs[2]!.querySelector(".project-tab-updates-badge")).toBeNull();
  });

  it("Add button calls onAdd when enabled and is a no-op when disabled", () => {
    const onAdd = vi.fn();
    const { container } = render(<ProjectTabs {...makeProps({ onAdd })} />);
    fireEvent.click(container.querySelector(".project-tab-add")!);
    expect(onAdd).toHaveBeenCalledTimes(1);

    const onAdd2 = vi.fn();
    const { container: c2 } = render(<ProjectTabs {...makeProps({ onAdd: onAdd2, disabled: true })} />);
    fireEvent.click(c2.querySelector(".project-tab-add")!);
    expect(onAdd2).not.toHaveBeenCalled();
  });

  it("Manage button opens the list modal", () => {
    const { container } = render(<ProjectTabs {...makeProps()} />);
    expect(container.querySelector(".project-list-modal")).toBeNull();
    fireEvent.click(container.querySelector(".project-tab-manage")!);
    expect(container.querySelector(".project-list-modal")).not.toBeNull();
  });

  it("opens the config menu at the button rect and toggles it closed on a second click", () => {
    const projects = [project({ path: "/x", node_id: "n1" })];
    const { container } = render(<ProjectTabs {...makeProps({ projects })} />);

    const config = container.querySelector(".project-tab-config")!;
    // A real click is mousedown→click; fire both so the stopPropagation
    // onMouseDown handler is exercised.
    fireEvent.mouseDown(config);
    fireEvent.click(config);
    expect(container.querySelector(".project-tab-menu")).not.toBeNull();

    fireEvent.mouseDown(config);
    fireEvent.click(config);
    expect(container.querySelector(".project-tab-menu")).toBeNull();
  });

  it("menu Settings item calls onOpenSettings and closes; Remove item calls onRemove and closes", () => {
    const onOpenSettings = vi.fn();
    const onRemove = vi.fn();
    const projects = [project({ path: "/x", node_id: "n1" })];
    const { container } = render(
      <ProjectTabs {...makeProps({ projects, onOpenSettings, onRemove })} />,
    );

    fireEvent.click(container.querySelector(".project-tab-config")!);
    // Exercise the menu's onMouseDown stopPropagation.
    fireEvent.mouseDown(container.querySelector(".project-tab-menu")!);
    fireEvent.click(container.querySelector(".project-tab-menu-item")!);
    expect(onOpenSettings).toHaveBeenCalledWith("/x", "n1");
    expect(container.querySelector(".project-tab-menu")).toBeNull();

    fireEvent.click(container.querySelector(".project-tab-config")!);
    fireEvent.mouseDown(container.querySelector(".project-tab-menu")!);
    fireEvent.click(container.querySelector(".project-tab-menu-item.danger")!);
    expect(onRemove).toHaveBeenCalledWith("/x", "n1");
    expect(container.querySelector(".project-tab-menu")).toBeNull();
  });

  it("menu items use the primary node fallback when node_id is absent", () => {
    const onOpenSettings = vi.fn();
    const onRemove = vi.fn();
    const projects = [project({ path: "/x" })]; // node_id undefined → "primary"
    const { container } = render(
      <ProjectTabs {...makeProps({ projects, onOpenSettings, onRemove })} />,
    );

    fireEvent.click(container.querySelector(".project-tab-config")!);
    fireEvent.click(container.querySelector(".project-tab-menu-item")!);
    expect(onOpenSettings).toHaveBeenCalledWith("/x", "primary");

    fireEvent.click(container.querySelector(".project-tab-config")!);
    fireEvent.click(container.querySelector(".project-tab-menu-item.danger")!);
    expect(onRemove).toHaveBeenCalledWith("/x", "primary");
  });

  it("closes the config menu on outside mousedown, Escape, resize, and scroll", () => {
    const projects = [project({ path: "/x", node_id: "n1" })];

    // outside mousedown
    const { container, unmount } = render(<ProjectTabs {...makeProps({ projects })} />);
    fireEvent.click(container.querySelector(".project-tab-config")!);
    // A non-Escape key must NOT close the menu (exercises the if's else side).
    pressKey("a");
    expect(container.querySelector(".project-tab-menu")).not.toBeNull();
    fireEvent.mouseDown(document.body);
    expect(container.querySelector(".project-tab-menu")).toBeNull();
    unmount();

    // Escape
    const r2 = render(<ProjectTabs {...makeProps({ projects })} />);
    fireEvent.click(r2.container.querySelector(".project-tab-config")!);
    pressKey("Escape");
    expect(r2.container.querySelector(".project-tab-menu")).toBeNull();
    r2.unmount();

    // resize
    const r3 = render(<ProjectTabs {...makeProps({ projects })} />);
    fireEvent.click(r3.container.querySelector(".project-tab-config")!);
    dispatchWin("resize");
    expect(r3.container.querySelector(".project-tab-menu")).toBeNull();
    r3.unmount();

    // scroll (capture)
    const r4 = render(<ProjectTabs {...makeProps({ projects })} />);
    fireEvent.click(r4.container.querySelector(".project-tab-config")!);
    dispatchWin("scroll");
    expect(r4.container.querySelector(".project-tab-menu")).toBeNull();
  });

  it("hides the menu when its project disappears on rerender (menuProject falsy guard)", () => {
    const projects = [project({ path: "/x", node_id: "n1" })];
    const { container, rerender } = render(<ProjectTabs {...makeProps({ projects })} />);
    fireEvent.click(container.querySelector(".project-tab-config")!);
    expect(container.querySelector(".project-tab-menu")).not.toBeNull();

    rerender(<ProjectTabs {...makeProps({ projects: [] })} />);
    expect(container.querySelector(".project-tab-menu")).toBeNull();
  });

  it("manage modal: select-all toggles every row, per-row toggles individually, label fallbacks resolve, and content mousedown does not close", () => {
    const projects = [
      project({ path: "/a", name: "A", node_id: "n1" }),
      project({ path: "/b/bar", name: "" }), // → projectPathName fallback "PN:/b/bar"
      project({ path: "/onlypath", name: "" }), // → raw path fallback
    ];
    const { container } = render(<ProjectTabs {...makeProps({ projects })} />);
    fireEvent.click(container.querySelector(".project-tab-manage")!);

    // Label fallbacks in the modal rows.
    const rowLabels = () =>
      Array.from(container.querySelectorAll(".project-list-modal-name")).map((n) => n.textContent);
    expect(rowLabels()).toEqual(["A", "PN:/b/bar", "/onlypath"]);

    const selectAll = container.querySelector(".project-list-select-all input") as HTMLInputElement;
    const rowChecks = () =>
      Array.from(container.querySelectorAll(".project-list-modal-row input")) as HTMLInputElement[];
    const deleteBtn = () => container.querySelector(".project-list-modal-danger") as HTMLButtonElement;

    // No selection → delete disabled.
    expect(deleteBtn().disabled).toBe(true);

    // Check a single row individually (setProjectSelected add branch).
    fireEvent.click(rowChecks()[0]!);
    expect(rowChecks()[0]!.checked).toBe(true);
    expect(deleteBtn().disabled).toBe(false);

    // Uncheck it individually (delete branch).
    fireEvent.click(rowChecks()[0]!);
    expect(rowChecks()[0]!.checked).toBe(false);

    // Select all, then clear all via select-all.
    fireEvent.click(selectAll);
    expect(rowChecks().every((c) => c.checked)).toBe(true);
    fireEvent.click(selectAll);
    expect(rowChecks().every((c) => !c.checked)).toBe(true);
    expect(deleteBtn().disabled).toBe(true);

    // mousedown inside the modal content must NOT close (stopPropagation).
    fireEvent.mouseDown(container.querySelector(".project-list-modal")!);
    expect(container.querySelector(".project-list-modal")).not.toBeNull();
  });

  it("manage modal delete with none selected is a no-op", () => {
    const onRemove = vi.fn();
    const { container } = render(<ProjectTabs {...makeProps({ onRemove })} />);
    fireEvent.click(container.querySelector(".project-tab-manage")!);
    fireEvent.click(container.querySelector(".project-list-modal-danger")!);
    expect(onRemove).not.toHaveBeenCalled();
    expect(container.querySelector(".project-list-modal")).not.toBeNull();
  });

  it("manage modal delete: removes each selected project, guards against re-entry, blocks close while deleting, then closes on success", async () => {
    const projects = [
      project({ path: "/a", name: "A" }),
      project({ path: "/b", name: "B" }),
    ];
    let resolveRemove: (() => void)[] = [];
    const onRemove = vi.fn(
      () => new Promise<void>((r) => { resolveRemove.push(r); }),
    );
    const { container } = render(<ProjectTabs {...makeProps({ projects, onRemove })} />);

    fireEvent.click(container.querySelector(".project-tab-manage")!);
    // Select all rows.
    fireEvent.click(container.querySelector(".project-list-select-all input") as HTMLInputElement);

    const deleteBtn = () => container.querySelector(".project-list-modal-danger") as HTMLButtonElement;
    fireEvent.click(deleteBtn());

    // Deleting state: button label switches, row checkboxes + close disabled.
    await waitFor(() => expect(deleteBtn().textContent).toContain("projects.deletingSelected"));
    expect(
      (container.querySelector(".project-list-modal-row input") as HTMLInputElement).disabled,
    ).toBe(true);
    expect((container.querySelector(".modal-close") as HTMLButtonElement).disabled).toBe(true);

    // Re-entry guard: clicking delete again does not call onRemove extra times.
    fireEvent.click(deleteBtn());
    // Close guard: overlay / cancel / close / Escape all no-op while deleting.
    fireEvent.mouseDown(container.querySelector(".project-list-modal-overlay")!);
    fireEvent.click(container.querySelector(".project-list-modal-secondary")!);
    fireEvent.click(container.querySelector(".modal-close")!);
    pressKey("Escape");
    expect(container.querySelector(".project-list-modal")).not.toBeNull();

    // Resolve both removals sequentially (deleteSelectedProjects awaits in order).
    expect(onRemove).toHaveBeenCalledTimes(1);
    await act(async () => { resolveRemove[0]!(); await Promise.resolve(); });
    expect(onRemove).toHaveBeenCalledTimes(2);
    await act(async () => { resolveRemove[1]!(); await Promise.resolve(); });

    await waitFor(() =>
      expect(container.querySelector(".project-list-modal")).toBeNull(),
    );
  });

  it("manage modal closes via overlay mousedown, cancel button, and × button", () => {
    const projects = [project({ path: "/a", name: "A" })];

    // overlay
    const r1 = render(<ProjectTabs {...makeProps({ projects })} />);
    fireEvent.click(r1.container.querySelector(".project-tab-manage")!);
    fireEvent.mouseDown(r1.container.querySelector(".project-list-modal-overlay")!);
    expect(r1.container.querySelector(".project-list-modal")).toBeNull();
    r1.unmount();

    // cancel
    const r2 = render(<ProjectTabs {...makeProps({ projects })} />);
    fireEvent.click(r2.container.querySelector(".project-tab-manage")!);
    fireEvent.click(r2.container.querySelector(".project-list-modal-secondary")!);
    expect(r2.container.querySelector(".project-list-modal")).toBeNull();
    r2.unmount();

    // × close
    const r3 = render(<ProjectTabs {...makeProps({ projects })} />);
    fireEvent.click(r3.container.querySelector(".project-tab-manage")!);
    fireEvent.click(r3.container.querySelector(".modal-close")!);
    expect(r3.container.querySelector(".project-list-modal")).toBeNull();
  });

  it("manage modal closes on Escape when not deleting", () => {
    const { container } = render(<ProjectTabs {...makeProps()} />);
    fireEvent.click(container.querySelector(".project-tab-manage")!);
    pressKey("Escape");
    expect(container.querySelector(".project-list-modal")).toBeNull();
  });

  it("manage modal with no projects keeps select-all disabled and unchecked", () => {
    const { container } = render(<ProjectTabs {...makeProps({ projects: [] })} />);
    fireEvent.click(container.querySelector(".project-tab-manage")!);
    const selectAll = container.querySelector(".project-list-select-all input") as HTMLInputElement;
    expect(selectAll.disabled).toBe(true);
    expect(selectAll.checked).toBe(false);
    fireEvent.click(container.querySelector(".modal-close")!);
  });
});
