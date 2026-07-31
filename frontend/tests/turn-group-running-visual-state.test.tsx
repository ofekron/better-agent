import { afterEach, cleanup, describe, expect, it } from "vitest";
import { cleanup, render } from "@testing-library/react";
import "../src/i18n";
import { TurnGroup } from "../src/components/MessageBubble";
import { makeAssistantMsg, makeUserMsg } from "./fixtures";

afterEach(cleanup);

function renderTurn({
  isStreaming = false,
  sessionRunning = false,
}: {
  isStreaming?: boolean;
  sessionRunning?: boolean;
}) {
  return render(
    <TurnGroup
      initiatorMessage={makeUserMsg({ id: "user-turn", content: "Inspect it" })}
      responseMessage={makeAssistantMsg({
        id: "assistant-turn",
        content: "Working result",
        isStreaming,
      })}
      orchestrationMode="native"
      sessionRunning={sessionRunning}
    />,
  ).container;
}

describe("turn-group running visual state", () => {
  it("keeps completed history visually inactive", () => {
    const container = renderTurn({});
    expect(
      container.querySelector(".turn-group")?.classList.contains("is-running"),
    ).toBe(false);
  });

  it("marks a streaming turn without changing its rendered content", () => {
    const container = renderTurn({ isStreaming: true });
    expect(
      container.querySelector(".turn-group")?.classList.contains("is-running"),
    ).toBe(true);
    expect(container.textContent).toContain("Inspect it");
  });

  it("uses the existing session-running authority", () => {
    const container = renderTurn({ sessionRunning: true });
    expect(
      container.querySelector(".turn-group")?.classList.contains("is-running"),
    ).toBe(true);
  });
});
