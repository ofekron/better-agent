// FileDiscussionPanel is a self-contained discussion thread UI: collapse
// toggle, title resolution, message filtering (by file_discussion_id across
// merged messages + pendingMessages), draft input, and a guarded submit
// (meta/ctrl+Enter or Send button). MessageBubble owns its own coverage, so
// it is mocked to a spy here — this isolates the panel's own logic AND lets us
// assert the props it forwards (key/message/sessionId/threadColorMap).
vi.mock("../src/components/MessageBubble", () => ({
  MessageBubble: vi.fn(() => null),
}));

import { afterEach, describe, expect, it, vi } from "vitest";
import { createEvent, fireEvent, render, waitFor } from "@testing-library/react";
import type { ChatMessage, FileDiscussion } from "../src/types";
import { FileDiscussionPanel } from "../src/components/FileDiscussionPanel";
import { MessageBubble } from "../src/components/MessageBubble";

const mockedMessageBubble = vi.mocked(MessageBubble);

function msg(partial: Partial<ChatMessage> & Pick<ChatMessage, "id">): ChatMessage {
  return {
    role: "user",
    content: "",
    events: [],
    timestamp: "",
    isStreaming: false,
    file_discussion_id: "d1",
    ...partial,
  };
}

function discussion(over: Partial<FileDiscussion> = {}): FileDiscussion {
  return { id: "d1", file_path: "src/a.ts", line: 10, ...over };
}

function defaultProps(over: Record<string, unknown> = {}) {
  return {
    discussion: discussion(),
    messages: [],
    pendingMessages: [],
    sessionId: "sess-1",
    onSend: vi.fn(async () => {}),
    onToggleCollapsed: vi.fn(async () => {}),
    ...over,
  };
}

afterEach(() => {
  vi.clearAllMocks();
});

function panel() {
  return document.querySelector(".file-discussion-panel") as HTMLElement;
}
function collapseButton() {
  return document.querySelector(".file-discussion-collapse") as HTMLButtonElement;
}
function sendButton() {
  return document.querySelector(".btn-small") as HTMLButtonElement;
}
function textarea() {
  return document.querySelector(".file-discussion-input") as HTMLTextAreaElement;
}

describe("FileDiscussionPanel — collapse + title", () => {
  it("renders expanded: body present, collapse button shows ▼ + Collapse label", () => {
    render(<FileDiscussionPanel {...defaultProps()} />);
    expect(panel().className).toContain("file-discussion-panel");
    expect(panel().className).not.toContain("collapsed");
    expect(panel().querySelector(".file-discussion-body")).toBeTruthy();
    expect(collapseButton().textContent).toBe("▼");
    expect(collapseButton().getAttribute("aria-label")).toBe("Collapse discussion");
  });

  it("renders collapsed: no body, className has collapsed, button shows ▶ + Expand label", () => {
    render(<FileDiscussionPanel {...defaultProps({ discussion: discussion({ collapsed: true }) })} />);
    expect(panel().className).toContain("collapsed");
    expect(panel().querySelector(".file-discussion-body")).toBeNull();
    expect(collapseButton().textContent).toBe("▶");
    expect(collapseButton().getAttribute("aria-label")).toBe("Expand discussion");
  });

  it("uses discussion.title when present", () => {
    render(<FileDiscussionPanel {...defaultProps({ discussion: discussion({ title: "My Thread" }) })} />);
    expect(panel().querySelector(".file-discussion-title")!.textContent).toBe("My Thread");
  });

  it("falls back to `Line <n> discussion` when title absent", () => {
    render(<FileDiscussionPanel {...defaultProps({ discussion: discussion({ line: 42 }) })} />);
    expect(panel().querySelector(".file-discussion-title")!.textContent).toBe("Line 42 discussion");
  });
});

describe("FileDiscussionPanel — collapse toggle wiring", () => {
  it("clicks collapse (expanded→collapsed): calls onToggleCollapsed(id, true)", () => {
    const onToggleCollapsed = vi.fn(async () => {});
    render(<FileDiscussionPanel {...defaultProps({ onToggleCollapsed })} />);
    fireEvent.click(collapseButton());
    expect(onToggleCollapsed).toHaveBeenCalledWith("d1", true);
  });

  it("clicks expand (collapsed→expanded): calls onToggleCollapsed(id, false)", () => {
    const onToggleCollapsed = vi.fn(async () => {});
    render(
      <FileDiscussionPanel
        {...defaultProps({ discussion: discussion({ collapsed: true }), onToggleCollapsed })}
      />,
    );
    fireEvent.click(collapseButton());
    expect(onToggleCollapsed).toHaveBeenCalledWith("d1", false);
  });

  it("onToggleCollapsed omitted: click does not throw and is a no-op", () => {
    render(<FileDiscussionPanel {...defaultProps({ onToggleCollapsed: undefined })} />);
    expect(() => fireEvent.click(collapseButton())).not.toThrow();
  });
});

describe("FileDiscussionPanel — thread message filter", () => {
  it("renders only messages whose file_discussion_id matches, merging messages + pendingMessages", () => {
    const messages = [
      msg({ id: "m1", file_discussion_id: "d1" }),
      msg({ id: "m2", file_discussion_id: "other" }),
    ];
    const pendingMessages = [
      msg({ id: "p1", file_discussion_id: "d1" }),
      msg({ id: "p2", file_discussion_id: null }),
    ];
    render(<FileDiscussionPanel {...defaultProps({ messages, pendingMessages })} />);
    const ids = mockedMessageBubble.mock.calls.map((c) => (c[0] as { message: ChatMessage }).message.id);
    expect(ids).toEqual(["m1", "p1"]);
  });

  it("forwards sessionId and an empty threadColorMap to each MessageBubble", () => {
    const messages = [msg({ id: "m1", file_discussion_id: "d1" })];
    render(<FileDiscussionPanel {...defaultProps({ messages, sessionId: "sess-9" })} />);
    const last = mockedMessageBubble.mock.calls.at(-1)![0] as {
      sessionId?: string;
      threadColorMap: Map<string, string>;
    };
    expect(last.sessionId).toBe("sess-9");
    expect(last.threadColorMap).toBeInstanceOf(Map);
    expect(last.threadColorMap.size).toBe(0);
  });

  it("recomputes the filter when the discussion id changes", () => {
    const messages = [
      msg({ id: "m1", file_discussion_id: "d1" }),
      msg({ id: "m2", file_discussion_id: "d2" }),
    ];
    const { rerender } = render(<FileDiscussionPanel {...defaultProps({ messages })} />);
    expect(mockedMessageBubble.mock.calls.map((c) => (c[0] as { message: ChatMessage }).message.id)).toEqual(["m1"]);
    rerender(<FileDiscussionPanel {...defaultProps({ discussion: discussion({ id: "d2" }), messages })} />);
    expect(mockedMessageBubble.mock.calls.map((c) => (c[0] as { message: ChatMessage }).message.id)).toEqual(["m1", "m2"]);
  });
});

describe("FileDiscussionPanel — draft + send button", () => {
  it("send button is disabled when draft is empty, enabled after typing", () => {
    render(<FileDiscussionPanel {...defaultProps()} />);
    expect(sendButton().disabled).toBe(true);
    fireEvent.change(textarea(), { target: { value: "hello" } });
    expect(sendButton().disabled).toBe(false);
  });

  it("click send: trims draft, calls onSend(id, prompt, clientId), clears draft, clientId has the file-discussion- prefix", async () => {
    const onSend = vi.fn(async () => {});
    render(<FileDiscussionPanel {...defaultProps({ onSend })} />);
    fireEvent.change(textarea(), { target: { value: "  hi there  " } });
    fireEvent.click(sendButton());
    await Promise.resolve();
    expect(onSend).toHaveBeenCalledTimes(1);
    const [dId, prompt, clientId] = onSend.mock.calls[0];
    expect(dId).toBe("d1");
    expect(prompt).toBe("hi there");
    expect(typeof clientId).toBe("string");
    expect(clientId).toMatch(/^file-discussion-/);
    expect(textarea().value).toBe("");
  });

  it("two sends produce distinct clientIds", async () => {
    const onSend = vi.fn(async () => {});
    render(<FileDiscussionPanel {...defaultProps({ onSend })} />);
    for (const text of ["first", "second"]) {
      fireEvent.change(textarea(), { target: { value: text } });
      fireEvent.click(sendButton());
      await Promise.resolve();
    }
    expect(onSend).toHaveBeenCalledTimes(2);
    const ids = onSend.mock.calls.map((c) => c[2]);
    expect(ids[0]).not.toBe(ids[1]);
  });

  it("whitespace-only draft: send disabled, submit no-op via Enter", () => {
    const onSend = vi.fn(async () => {});
    render(<FileDiscussionPanel {...defaultProps({ onSend })} />);
    fireEvent.change(textarea(), { target: { value: "   " } });
    expect(sendButton().disabled).toBe(true);
    // meta+Enter on whitespace still resolves to empty prompt -> submit early-returns
    fireEvent.keyDown(textarea(), { key: "Enter", metaKey: true });
    expect(onSend).not.toHaveBeenCalled();
  });

  it("onSend omitted: typing + click does not throw, no call happens", () => {
    render(<FileDiscussionPanel {...defaultProps({ onSend: undefined })} />);
    fireEvent.change(textarea(), { target: { value: "hi" } });
    expect(() => fireEvent.click(sendButton())).not.toThrow();
  });
});

describe("FileDiscussionPanel — keyboard submit + in-flight guard", () => {
  it("meta+Enter submits and calls preventDefault", async () => {
    const onSend = vi.fn(async () => {});
    render(<FileDiscussionPanel {...defaultProps({ onSend })} />);
    fireEvent.change(textarea(), { target: { value: "hi" } });
    const event = createEvent.keyDown(textarea(), { key: "Enter", metaKey: true });
    fireEvent(textarea(), event);
    expect(event.defaultPrevented).toBe(true);
    await Promise.resolve();
    expect(onSend).toHaveBeenCalledTimes(1);
    expect(onSend.mock.calls[0][1]).toBe("hi");
  });

  it("ctrl+Enter submits", async () => {
    const onSend = vi.fn(async () => {});
    render(<FileDiscussionPanel {...defaultProps({ onSend })} />);
    fireEvent.change(textarea(), { target: { value: "hi" } });
    const event = createEvent.keyDown(textarea(), { key: "Enter", ctrlKey: true });
    fireEvent(textarea(), event);
    expect(event.defaultPrevented).toBe(true);
    await Promise.resolve();
    expect(onSend).toHaveBeenCalledTimes(1);
  });

  it("plain Enter does not submit and does not preventDefault", () => {
    const onSend = vi.fn(async () => {});
    render(<FileDiscussionPanel {...defaultProps({ onSend })} />);
    fireEvent.change(textarea(), { target: { value: "hi" } });
    const event = createEvent.keyDown(textarea(), { key: "Enter" });
    fireEvent(textarea(), event);
    expect(event.defaultPrevented).toBe(false);
    expect(onSend).not.toHaveBeenCalled();
  });

  it("disables the Send button while onSend is in flight, then re-enables; guards re-entry", async () => {
    let resolveSend: () => void = () => {};
    const onSend = vi.fn(
      () => new Promise<void>((resolve) => {
        resolveSend = resolve;
      }),
    );
    render(<FileDiscussionPanel {...defaultProps({ onSend })} />);
    fireEvent.change(textarea(), { target: { value: "hi" } });
    expect(sendButton().disabled).toBe(false);

    fireEvent.click(sendButton());
    // draft cleared and sending=true for the duration of the in-flight promise
    expect(textarea().value).toBe("");
    expect(sendButton().disabled).toBe(true);
    expect(onSend).toHaveBeenCalledTimes(1);

    // a second submit attempt while in flight must be a no-op (sending guard)
    fireEvent.change(textarea(), { target: { value: "again" } });
    // sending guard: submit() early-returns because `sending` is still true.
    // Draft was just re-set, but submit won't fire — simulate Enter.
    fireEvent.keyDown(textarea(), { key: "Enter", metaKey: true });
    expect(onSend).toHaveBeenCalledTimes(1);

    resolveSend();
    // settled: button enabled again for a fresh draft (state flushes async)
    await waitFor(() => expect(sendButton().disabled).toBe(false));
  });
});
