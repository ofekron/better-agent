import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { InputArea, splitPromptMentionParts } from "../src/components/InputArea";
import type { MentionItem } from "../src/components/AtMentionDropdown";
import type { Project, Session } from "../src/types";

function makeProject(overrides: Partial<Project> = {}): Project {
  return {
    path: "/Users/test/project-alpha",
    name: "project-alpha",
    node_id: "primary",
    created_at: "2026-01-01T00:00:00",
    last_used: "2026-01-01T00:00:00",
    ...overrides,
  };
}

function makeSession(overrides: Partial<Session> = {}): Session {
  return {
    id: "s1",
    name: "Research thread",
    model: "claude",
    cwd: "/Users/test/research",
    messages: [],
    created_at: "2026-01-01T00:00:00",
    updated_at: "2026-01-01T00:00:00",
    ...overrides,
  };
}

describe("splitPromptMentionParts", () => {
  it("marks known inserted mentions without changing surrounding text", () => {
    const mentions: MentionItem[] = [
      {
        id: "project:/Users/test/project-alpha",
        label: "project-alpha",
        secondary: "/Users/test/project-alpha",
        kind: "project",
        nodeId: "primary",
      },
      {
        id: "session:s1",
        label: "Research thread",
        secondary: "/Users/test/research",
        kind: "session",
        nodeId: "primary",
      },
    ];

    expect(
      splitPromptMentionParts(
        "Use project-alpha (/Users/test/project-alpha) with Research thread (/Users/test/research).",
        mentions,
      ),
    ).toEqual([
      { kind: "text", text: "Use " },
      {
        kind: "mention",
        text: "project-alpha (/Users/test/project-alpha)",
        mentionKind: "project",
      },
      { kind: "text", text: " with " },
      {
        kind: "mention",
        text: "Research thread (/Users/test/research)",
        mentionKind: "session",
      },
      { kind: "text", text: "." },
    ]);
  });
});

describe("InputArea mention rendering", () => {
  it("renders known prompt mentions as special highlight spans", () => {
    render(
      <InputArea
        onSend={vi.fn()}
        isStreaming={false}
        disabled={false}
        draft="Check project-alpha (/Users/test/project-alpha)"
        onDraftChange={vi.fn()}
        queuedPrompt={null}
        onPromoteQueued={vi.fn()}
        projects={[makeProject()]}
        sessions={[makeSession()]}
      />,
    );

    const highlight = screen.getByTestId("input-mention-highlight");
    const mention = highlight.querySelector(".input-prompt-mention.kind-project");

    expect(mention?.textContent).toBe("project-alpha (/Users/test/project-alpha)");
    expect((screen.getByTestId("input-textarea") as HTMLTextAreaElement).value).toBe(
      "Check project-alpha (/Users/test/project-alpha)",
    );
  });
});
