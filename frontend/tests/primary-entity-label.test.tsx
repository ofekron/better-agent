import { describe, it, expect, afterEach } from "vitest";
import { render, cleanup } from "@testing-library/react";
import React from "react";
import { MessageGroup } from "../src/components/MessageBubble";
import { makeAssistantMsg, makeUserMsg } from "./fixtures";

afterEach(cleanup);

function toggleLabels(container: HTMLElement): (string | null)[] {
  return Array.from(
    container.querySelectorAll(".timeline-toggle-label"),
  ).map((el) => el.textContent);
}

describe("primary-entity toggle label by orchestration mode", () => {
  it("native turn never renders a manager scope chip", () => {
    const { container } = render(
      <MessageGroup
        userMessage={makeUserMsg({ id: "u1", content: "hi" })}
        assistantMessage={makeAssistantMsg({
          id: "a1",
          content: "",
          events: [{ type: "output", data: { output: "ok" } }],
          manager: undefined, // native shape: no manager scope
        })}
        orchestrationMode="native"
      />,
    );
    expect(toggleLabels(container)).not.toContain("Team");
    expect(container.querySelector(".role-label-manager")).toBeNull();
    expect(container.querySelector(".timeline-entity-header .role-chip:not(.role-chip-worker)")).toBeNull();
  });

  it("team turn is labeled 'Team'", () => {
    const { container } = render(
      <MessageGroup
        userMessage={makeUserMsg({ id: "u1", content: "hi" })}
        assistantMessage={makeAssistantMsg({
          id: "a1",
          content: "",
          events: [{ type: "output", data: { output: "ok" } }],
          manager: { session_id: "claude-x", events: [] },
        })}
        orchestrationMode="team"
      />,
    );
    expect(container.querySelector(".role-label-manager .role-chip")?.textContent).toBe("Team");
  });

  it("native turn with sub-session panels keeps primary output unlabeled", () => {
    const { container } = render(
      <MessageGroup
        userMessage={makeUserMsg({ id: "u1", content: "hi" })}
        assistantMessage={makeAssistantMsg({
          id: "a1",
          content: "",
          manager: undefined,
          events: [
            { type: "output", data: { output: "primary before" } },
            { type: "output", data: { output: "primary after" } },
          ],
          workers: [
            {
              delegation_id: "sub-a",
              worker_session_id: "sub-session-id",
              worker_description: "Review Subsession",
              panel_kind: "sub_session",
              insert_at: 1,
              is_new: false,
              instructions_preview: "",
              events: [
                { type: "output", data: { output: "subsession output" } },
              ],
            },
          ],
        })}
        orchestrationMode="native"
      />,
    );

    const text = container.textContent ?? "";
    expect(text.indexOf("primary before")).toBeLessThan(text.indexOf("Review Subsession"));
    expect(text.indexOf("Review Subsession")).toBeLessThan(text.indexOf("primary after"));
    expect(container.querySelector(".role-label-manager")).toBeNull();
    expect(container.querySelector(".timeline-entity-header .role-chip:not(.role-chip-worker)")).toBeNull();
  });
});
