import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, within } from "@testing-library/react";
import { SharePicker } from "../src/components/SharePicker";
import type { PastedImage, Project, Session } from "../src/types";
import "../src/i18n";

const img = (id: string): PastedImage => ({
  dataUrl: `data:image/jpeg;base64,${id}`,
  base64: id,
  mediaType: "image/jpeg",
});

const sess = (id: string, cwd: string, updated_at: string): Session =>
  ({ id, name: `name-${id}`, cwd, updated_at, messages: [] } as unknown as Session);

const projects: Project[] = [
  { path: "/proj/a", name: "Alpha", created_at: "x", last_used: "x" },
  { path: "/proj/b", name: "Beta", created_at: "x", last_used: "x" },
];

// 6 sessions across two projects with distinct updated_at timestamps.
const sessions: Session[] = [
  sess("s1", "/proj/a", "2026-01-01T00:00:00Z"),
  sess("s2", "/proj/a", "2026-06-01T00:00:00Z"),
  sess("s3", "/proj/b", "2026-03-01T00:00:00Z"),
  sess("s4", "/proj/b", "2026-05-01T00:00:00Z"),
  sess("s5", "/proj/a", "2026-04-01T00:00:00Z"),
  sess("s6", "/proj/b", "2026-02-01T00:00:00Z"),
];

function renderPicker(images: PastedImage[], onPick = vi.fn(), onCancel = vi.fn()) {
  render(
    <SharePicker
      images={images}
      projects={projects}
      sessions={sessions}
      onPick={onPick}
      onCancel={onCancel}
    />
  );
  return { onPick, onCancel };
}

describe("SharePicker", () => {
  it("renders a thumbnail per shared image (SEND_MULTIPLE)", () => {
    renderPicker([img("a"), img("b"), img("c")]);
    const thumbs = screen.getByTestId("share-thumbs");
    expect(within(thumbs).getAllByRole("img")).toHaveLength(3);
  });

  it("recent row shows the 5 most-recent sessions by updated_at desc", () => {
    renderPicker([img("a")]);
    const recent = screen.getByTestId("share-recent");
    const labels = within(recent)
      .getAllByTestId("share-recent-session")
      .map((b) => b.textContent);
    // updated_at order: s2(Jun) s4(May) s5(Apr) s3(Mar) s6(Feb) [s1(Jan) dropped]
    expect(labels).toEqual([
      "name-s2",
      "name-s4",
      "name-s5",
      "name-s3",
      "name-s6",
    ]);
  });

  it("tapping a recent session calls onPick with its id", () => {
    const { onPick } = renderPicker([img("a")]);
    const recent = screen.getByTestId("share-recent");
    fireEvent.click(within(recent).getAllByTestId("share-recent-session")[0]);
    expect(onPick).toHaveBeenCalledWith("s2");
  });

  it("drilling a project lists only that project's sessions (cwd filter)", () => {
    const { onPick } = renderPicker([img("a")]);
    // Open project Alpha (/proj/a) → expect s1, s2, s5 (cwd === /proj/a).
    fireEvent.click(screen.getAllByTestId("share-project")[0]);
    const drill = screen.getByTestId("share-project-sessions");
    const labels = within(drill)
      .getAllByTestId("share-project-session")
      .map((b) => b.textContent);
    expect(labels).toEqual(["name-s2", "name-s5", "name-s1"]); // recency desc
    fireEvent.click(within(drill).getAllByTestId("share-project-session")[0]);
    expect(onPick).toHaveBeenCalledWith("s2");
  });
});
