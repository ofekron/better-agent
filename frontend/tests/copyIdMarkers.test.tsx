import { describe, it, expect, vi } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import {
  sessionLinkMarker,
  eventLinkMarker,
  baMarkersToMarkdown,
  markdownLinkifyComponents,
} from "../src/utils/linkifyFilePaths";

const focus = vi.hoisted(() => ({ requestMessageFocus: vi.fn() }));
vi.mock("../src/utils/messageFocus", () => ({
  requestMessageFocus: focus.requestMessageFocus,
}));

const { a: Anchor } = markdownLinkifyComponents();

describe("copy-id reference markers", () => {
  it("session marker carries the id and converts to a session link", () => {
    const marker = sessionLinkMarker("sid-123", "My Session");
    expect(marker).toBe("[[ba-session:sid-123|My%20Session]]");
    // The raw id survives so an agent can resolve it from pasted text.
    expect(decodeURIComponent(marker.split(":")[1].split("|")[0])).toBe("sid-123");
    expect(baMarkersToMarkdown(`see ${marker}`)).toBe("see [My Session · sid-](/s/sid-123)");
  });

  it("event marker carries both ids and converts to a message-anchored link", () => {
    const marker = eventLinkMarker("sid-123", "msg-abcdef7", "");
    expect(marker).toBe("[[ba-event:sid-123|msg-abcdef7|]]");
    expect(baMarkersToMarkdown(marker)).toBe("[Event · msg-ab](/s/sid-123?m=msg-abcdef7)");
  });

  it("encodes separator/bracket chars so fields never break the split", () => {
    const marker = sessionLinkMarker("a|b]c", "n|m");
    // pipes and brackets are percent-encoded, so exactly two fields remain.
    const body = marker.slice("[[ba-session:".length, -"]]".length);
    expect(body.split("|")).toHaveLength(2);
    expect(baMarkersToMarkdown(marker)).toBe("[n|m · a|b]](/s/a%7Cb%5Dc)");
  });

  it("renders an event href as a link that requests message focus on click", () => {
    const { container } = render(<Anchor href="/s/sid-9?m=msg-9">jump</Anchor>);
    const link = container.querySelector('[role="link"]');
    expect(link).not.toBeNull();
    fireEvent.click(link!);
    expect(focus.requestMessageFocus).toHaveBeenCalledWith("sid-9", "msg-9");
  });
});
