import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import {
  AtMentionDropdown,
  buildMentionItems,
  formatMentionInsert,
  type MentionItem,
} from "../src/components/AtMentionDropdown";
import type { Project, Session } from "../src/types";

function makeProject(overrides: Partial<Project> = {}): Project {
  return {
    path: "/Users/test/my-project",
    name: "my-project",
    node_id: "primary",
    created_at: "2026-01-01T00:00:00",
    last_used: "2026-01-01T00:00:00",
    ...overrides,
  };
}

function makeSession(overrides: Partial<Session> = {}): Session {
  return {
    id: "s1",
    name: "Test Session",
    model: "claude-3",
    cwd: "/Users/test/my-project",
    messages: [],
    created_at: "2026-01-01T00:00:00",
    updated_at: "2026-01-01T00:00:00",
    ...overrides,
  };
}

describe("buildMentionItems", () => {
  it("builds items from projects and sessions", () => {
    const projects = [makeProject()];
    const sessions = [makeSession()];
    const items = buildMentionItems(projects, sessions);

    expect(items).toHaveLength(2);
    expect(items[0].kind).toBe("project");
    expect(items[0].label).toBe("my-project");
    expect(items[0].secondary).toBe("/Users/test/my-project");
    expect(items[1].kind).toBe("session");
    expect(items[1].label).toBe("Test Session");
  });

  it("skips sessions without cwd", () => {
    const sessions = [makeSession({ cwd: "" })];
    const items = buildMentionItems([], sessions);
    expect(items).toHaveLength(0);
  });
});

describe("formatMentionInsert", () => {
  it("formats as 'name (path)'", () => {
    const item: MentionItem = {
      id: "p1",
      label: "my-project",
      secondary: "/path/to/project",
      kind: "project",
    };
    expect(formatMentionInsert(item)).toBe("my-project (/path/to/project)");
  });
});

describe("AtMentionDropdown", () => {
  it("renders filtered items", () => {
    const projects = [
      makeProject({ name: "alpha", path: "/a" }),
      makeProject({ name: "beta", path: "/b" }),
    ];
    const sessions = [makeSession({ name: "Gamma", cwd: "/g" })];
    const onSelect = vi.fn();
    const onClose = vi.fn();

    render(
      <AtMentionDropdown
        query="a"
        triggerStart={0}
        projects={projects}
        sessions={sessions}
        onSelect={onSelect}
        onClose={onClose}
      />,
    );

    // "a" matches "alpha" (project) and "Gamma" (session) — both contain 'a'
    // At least alpha should match
    expect(screen.getAllByText("alpha")).toHaveLength(1);
  });

  it("calls onSelect when item is clicked", async () => {
    const projects = [makeProject()];
    const onSelect = vi.fn();
    const onClose = vi.fn();

    render(
      <AtMentionDropdown
        query=""
        triggerStart={5}
        projects={projects}
        sessions={[]}
        onSelect={onSelect}
        onClose={onClose}
      />,
    );

    const item = screen.getByText("my-project").closest(".at-mention-item")!;
    fireEvent.mouseDown(item);

    expect(onSelect).toHaveBeenCalledWith(
      expect.objectContaining({ label: "my-project", kind: "project" }),
      5,
      6, // triggerStart(5) + 1(@) + 0(query length)
    );
  });

  it("calls onSelect with correct triggerEnd when query is non-empty", async () => {
    const projects = [makeProject()];
    const onSelect = vi.fn();
    const onClose = vi.fn();

    render(
      <AtMentionDropdown
        query="my"
        triggerStart={3}
        projects={projects}
        sessions={[]}
        onSelect={onSelect}
        onClose={onClose}
    />,
    );

    const item = screen.getByText("my-project").closest(".at-mention-item")!;
    fireEvent.mouseDown(item);

    expect(onSelect).toHaveBeenCalledWith(
      expect.objectContaining({ label: "my-project" }),
      3,
      6, // triggerStart(3) + 1(@) + 2(query length "my")
    );
  });

  it("returns null when no items match", () => {
    const { container } = render(
      <AtMentionDropdown
        query="zzzzz"
        triggerStart={0}
        projects={[]}
        sessions={[]}
        onSelect={vi.fn()}
        onClose={vi.fn()}
      />,
    );

    expect(container.innerHTML).toBe("");
  });
});
