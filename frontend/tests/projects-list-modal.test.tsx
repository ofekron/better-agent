import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import "../src/i18n";
import { ProjectTabs } from "../src/components/ProjectTabs";
import type { Project } from "../src/types";

const projects: Project[] = [
  {
    path: "/repos/alpha",
    node_id: "primary",
    name: "Alpha",
    created_at: "2026-01-01T00:00:00",
    last_used: "2026-01-01T00:00:00",
  },
  {
    path: "/repos/beta",
    node_id: "remote-1",
    name: "Beta",
    created_at: "2026-01-01T00:00:00",
    last_used: "2026-01-01T00:00:00",
  },
];

describe("ProjectTabs project list modal", () => {
  it("selects projects and deletes the selected rows", async () => {
    const onRemove = vi.fn().mockResolvedValue(undefined);

    render(
      <ProjectTabs
        projects={projects}
        currentPath="/repos/alpha"
        currentNodeId="primary"
        onSelect={vi.fn()}
        onAdd={vi.fn()}
        onRemove={onRemove}
        onOpenSettings={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Manage projects" }));

    expect(screen.getByRole("dialog", { name: "Manage projects" })).toBeTruthy();
    fireEvent.click(screen.getByLabelText(/Alpha/));
    fireEvent.click(screen.getByLabelText(/Beta/));
    fireEvent.click(screen.getByRole("button", { name: "Delete selected" }));

    await waitFor(() => {
      expect(onRemove).toHaveBeenCalledTimes(2);
    });
    expect(onRemove).toHaveBeenNthCalledWith(1, "/repos/alpha", "primary");
    expect(onRemove).toHaveBeenNthCalledWith(2, "/repos/beta", "remote-1");
    expect(screen.queryByRole("dialog", { name: "Manage projects" })).toBeNull();
  });
});
