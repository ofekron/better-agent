import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import React from "react";
import { FileDiscussionPanel } from "../src/components/FileDiscussionPanel";

describe("FileDiscussionPanel", () => {
  it("sends prompts with the discussion id and client id", async () => {
    const onSend = vi.fn(async () => {});
    render(
      <FileDiscussionPanel
        discussion={{
          id: "fd_1",
          file_path: "/tmp/a.ts",
          line: 12,
        }}
        messages={[]}
        pendingMessages={[]}
        onSend={onSend}
      />,
    );

    fireEvent.change(screen.getByRole("textbox"), {
      target: { value: "look here" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => expect(onSend).toHaveBeenCalledTimes(1));
    expect(onSend.mock.calls[0][0]).toBe("fd_1");
    expect(onSend.mock.calls[0][1]).toBe("look here");
    expect(onSend.mock.calls[0][2]).toMatch(/^file-discussion-/);
  });

  it("toggles collapsed state through the backend patch callback", () => {
    const onToggleCollapsed = vi.fn(async () => {});
    render(
      <FileDiscussionPanel
        discussion={{
          id: "fd_1",
          file_path: "/tmp/a.ts",
          line: 12,
          collapsed: true,
        }}
        messages={[]}
        pendingMessages={[]}
        onToggleCollapsed={onToggleCollapsed}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Expand discussion" }));

    expect(onToggleCollapsed).toHaveBeenCalledWith("fd_1", false);
  });
});
