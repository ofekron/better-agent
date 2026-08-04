import { describe, it, expect, vi } from "vitest";
import { cleanup, render, screen, fireEvent } from "@testing-library/react";
import type { FileNode } from "../src/types";

import { PickerNode } from "../src/components/PickerNode";

const fileNode: FileNode = { name: "a.txt", path: "/r/a.txt", type: "file" };
const dirNode: FileNode = {
  name: "d",
  path: "/r/d",
  type: "directory",
  children: [{ name: "child.txt", path: "/r/d/child.txt", type: "file" }],
};

describe("PickerNode", () => {
  it("renders a selected file node and fires select/activate", () => {
    const onSelect = vi.fn();
    const onActivate = vi.fn();
    const { container } = render(
      <PickerNode
        node={fileNode}
        depth={0}
        selected={fileNode.path}
        onSelect={onSelect}
        onActivate={onActivate}
      />,
    );
    const el = container.querySelector(".fp-file") as HTMLElement;
    expect(el).toBeTruthy();
    expect(el.className).toContain("fp-active");
    expect(el.getAttribute("title")).toBe(fileNode.path);
    expect(el.style.paddingInlineStart).toBe("8px");
    expect(screen.getByText("a.txt")).toBeTruthy();

    fireEvent.click(el);
    expect(onSelect).toHaveBeenCalledWith(fileNode);
    fireEvent.doubleClick(el);
    expect(onActivate).toHaveBeenCalledWith(fileNode);
    cleanup();
  });

  it("renders an unselected file node and tolerates a missing onActivate", () => {
    const { container } = render(
      <PickerNode node={fileNode} depth={0} selected="other" onSelect={vi.fn()} />,
    );
    const el = container.querySelector(".fp-file") as HTMLElement;
    expect(el.className).not.toContain("fp-active");
    expect(() => fireEvent.doubleClick(el)).not.toThrow();
    cleanup();
  });

  it("opens a depth-0 directory by default and collapses on click", () => {
    const onSelect = vi.fn();
    const { container } = render(
      <PickerNode node={dirNode} depth={0} selected="x" onSelect={onSelect} />,
    );
    const dirEl = container.querySelector(".fp-dir") as HTMLElement;
    expect(dirEl).toBeTruthy();
    expect(container.querySelector(".fp-arrow")?.textContent).toBe("▾");
    expect(screen.getByText("child.txt")).toBeTruthy();

    fireEvent.click(dirEl);
    expect(onSelect).toHaveBeenCalledWith(dirNode);
    expect(container.querySelector(".fp-arrow")?.textContent).toBe("▸");
    expect(screen.queryByText("child.txt")).toBeNull();
    cleanup();
  });

  it("starts collapsed at depth>=1, expands on click, and activates on dblclick", () => {
    const onActivate = vi.fn();
    const { container } = render(
      <PickerNode node={dirNode} depth={1} selected="x" onSelect={vi.fn()} onActivate={onActivate} />,
    );
    expect(container.querySelector(".fp-arrow")?.textContent).toBe("▸");
    expect(screen.queryByText("child.txt")).toBeNull();

    const dirEl = container.querySelector(".fp-dir") as HTMLElement;
    fireEvent.click(dirEl);
    expect(container.querySelector(".fp-arrow")?.textContent).toBe("▾");
    expect(screen.getByText("child.txt")).toBeTruthy();

    fireEvent.doubleClick(dirEl);
    expect(onActivate).toHaveBeenCalledWith(dirNode);
    cleanup();
  });

  it("stays expanded and keeps its toggle inert when forceExpanded", () => {
    const { container } = render(
      <PickerNode node={dirNode} depth={5} selected="x" onSelect={vi.fn()} forceExpanded />,
    );
    expect(container.querySelector(".fp-arrow")?.textContent).toBe("▾");
    expect(screen.getByText("child.txt")).toBeTruthy();

    fireEvent.click(container.querySelector(".fp-dir") as HTMLElement);
    expect(container.querySelector(".fp-arrow")?.textContent).toBe("▾");
    expect(screen.getByText("child.txt")).toBeTruthy();
    cleanup();
  });

  it("renders only the dir row when a directory has no children, and marks a selected dir active", () => {
    const emptyDir: FileNode = { name: "e", path: "/r/e", type: "directory" };
    const { container } = render(
      <PickerNode node={emptyDir} depth={0} selected={emptyDir.path} onSelect={vi.fn()} />,
    );
    expect(container.querySelectorAll(".fp-node").length).toBe(1);
    expect(
      (container.querySelector(".fp-dir") as HTMLElement).className,
    ).toContain("fp-active");
    cleanup();
  });
});
