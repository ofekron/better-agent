import { describe, it, expect, afterEach } from "vitest";
import { render, cleanup } from "@testing-library/react";
import React from "react";
import "../src/i18n";
import { MessageGroup, MessageBubble } from "../src/components/MessageBubble";
import { makeAssistantMsg, makeUserMsg } from "./fixtures";

afterEach(cleanup);

/**
 * Locks the requirement at the RENDER sites (not just the helper): an
 * injected (source-bearing) user prompt must never display "User", and a
 * genuine source-less prompt must display "User".
 */
describe("injected user-prompt label — paired render path (MessageGroup)", () => {
  function label(source?: string): string | null {
    const { container } = render(
      <MessageGroup
        userMessage={makeUserMsg({ id: "u1", content: "hi", source })}
        assistantMessage={makeAssistantMsg({ id: "a1", content: "ok" })}
        orchestrationMode="native"
      />,
    );
    return (
      container.querySelector(".user-message-box .message-box-label")
        ?.textContent ?? null
    );
  }

  it("source-less prompt is labeled User", () => {
    expect(label(undefined)).toBe("User");
  });

  it("team_ask is labeled Ask, never User", () => {
    expect(label("team_ask")).toBe("Ask");
  });

  it("team_message is labeled Message, never User", () => {
    expect(label("team_message")).toBe("Message");
  });

  it("an unknown injected source is humanized, never User", () => {
    expect(label("custom_bridge_source")).toBe("Custom Bridge Source");
  });
});

describe("injected user-prompt label — standalone render path (MessageBubble)", () => {
  it("renders an origin header for an injected source", () => {
    const { container } = render(
      <MessageBubble
        message={makeUserMsg({ id: "u1", content: "hi", source: "team_message" })}
      />,
    );
    const header = container.querySelector(".standalone-user-source .message-box-label");
    expect(header?.textContent).toBe("Message");
    expect(container.textContent).not.toContain("User");
  });

  it("renders no origin header (and no 'User') for a source-less prompt", () => {
    const { container } = render(
      <MessageBubble message={makeUserMsg({ id: "u1", content: "hi" })} />,
    );
    expect(container.querySelector(".standalone-user-source")).toBeNull();
  });
});
