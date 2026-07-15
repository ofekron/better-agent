import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import type { ComponentProps } from "react";
import "../src/i18n";
import { InputArea } from "../src/components/InputArea";

function setViewportWidth(width: number) {
  Object.defineProperty(window, "innerWidth", {
    configurable: true,
    writable: true,
    value: width,
  });
  window.dispatchEvent(new Event("resize"));
}

afterEach(() => {
  cleanup();
  window.localStorage.clear();
  setViewportWidth(1024);
  vi.restoreAllMocks();
});

beforeEach(() => {
  vi.spyOn(globalThis, "fetch").mockImplementation(() => new Promise(() => {}));
});

function renderInputArea(canSteer: boolean, draft = "", extra: Partial<ComponentProps<typeof InputArea>> = {}) {
  const onSend = vi.fn();
  const onSteer = vi.fn();
  const onInterrupt = vi.fn();
  const onPromoteQueued = vi.fn();
  const onSteerQueued = vi.fn();
  const onCancelQueued = vi.fn();
  const result = render(
    <InputArea
      onSend={onSend}
      onSteer={onSteer}
      onInterrupt={onInterrupt}
      canSteer={canSteer}
      isStreaming={true}
      disabled={false}
      sessionId="session-1"
      sessions={[{ id: "session-1", model: "gpt-5.4" } as never]}
      draft={draft}
      onDraftChange={vi.fn()}
      queuedPrompts={[{ id: "q1", preview: "queued work" }]}
      onPromoteQueued={onPromoteQueued}
      onSteerQueued={onSteerQueued}
      onCancelQueued={onCancelQueued}
      {...extra}
    />,
  );
  return { ...result, onSend, onSteer, onInterrupt, onPromoteQueued, onSteerQueued, onCancelQueued };
}

describe("InputArea queued prompt promote action", () => {
  it("shows separate queued Steer and Interrupt actions when the provider supports steering", () => {
    const { onPromoteQueued, onSteerQueued } = renderInputArea(true);

    const banner = screen.getByTestId("queued-prompt-banner");
    const steer = within(banner).getByRole("button", { name: "Steer" });
    const interrupt = within(banner).getByRole("button", { name: "⚡ Interrupt" });
    expect(steer.getAttribute("title")).toBe("Send into the active Codex turn");
    expect(interrupt.getAttribute("title")).toBe(
      "Cancel current turn and send this prompt immediately",
    );
    expect(interrupt.classList.contains("interrupt")).toBe(true);

    fireEvent.click(steer);
    expect(onSteerQueued).toHaveBeenCalledWith("q1");
    expect(onPromoteQueued).toHaveBeenCalledTimes(0);

    fireEvent.click(interrupt);
    expect(onPromoteQueued).toHaveBeenCalledWith("q1");
  });

  it("keeps Interrupt on queued prompts when steering is unavailable", () => {
    renderInputArea(false);

    const banner = screen.getByTestId("queued-prompt-banner");
    const button = within(banner).getByRole("button", { name: "⚡ Interrupt" });
    expect(within(banner).queryByRole("button", { name: "Steer" })).toBeNull();
    expect(button.getAttribute("title")).toBe(
      "Cancel current turn and send this prompt immediately",
    );
    expect(button.classList.contains("interrupt")).toBe(true);
  });

  it("renders every queued prompt package as its own banner", () => {
    const onQueuedTextEdit = vi.fn();
    renderInputArea(true, "", {
      queuedPrompts: [
        { id: "q1", preview: "first queued" },
        { id: "q2", preview: "second queued" },
      ],
      onQueuedTextEdit,
    });

    const banners = screen.getAllByTestId("queued-prompt-banner");
    expect(banners).toHaveLength(2);
    expect(within(banners[0]).getByText("first queued")).toBeTruthy();
    expect(within(banners[1]).getByText("second queued")).toBeTruthy();
    expect(within(banners[0]).getByRole("button", { name: "Steer" })).toBeTruthy();
    expect(within(banners[1]).getByRole("button", { name: "Steer" })).toBeTruthy();
    expect(within(banners[0]).getByRole("button", { name: "⚡ Interrupt" })).toBeTruthy();
    expect(within(banners[1]).getByRole("button", { name: "⚡ Interrupt" })).toBeTruthy();

    fireEvent.click(within(banners[1]).getByRole("button", { name: "second queued" }));
    const editor = screen.getByDisplayValue("second queued");
    fireEvent.change(editor, { target: { value: "edited second queued" } });
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
    expect(onQueuedTextEdit).toHaveBeenCalledWith("edited second queued", "q2");
  });

  it("shows one selected active action with the alternative in its picker", () => {
    renderInputArea(true, "active work");

    expect(screen.getByTestId("send-btn").textContent).toBe("Steer");
    expect(screen.getByTestId("queue-btn").textContent).toBe("Queue");
    expect(screen.queryByTestId("interrupt-btn")).toBeNull();

    fireEvent.click(document.querySelector(".active-action-picker-trigger")!);
    expect(screen.getByRole("button", { name: "Interrupt" })).toBeTruthy();
  });

  it("loads and persists the selected active action per model", async () => {
    const fetchMock = vi.mocked(globalThis.fetch)
      .mockReset()
      .mockImplementation(async (input, init) => {
        if (String(input) !== "/api/user-prefs") {
          return new Response(JSON.stringify({}), { status: 200 });
        }
        const action = init?.method === "PATCH" ? "steer" : "interrupt";
        return new Response(JSON.stringify({
          composer_active_action_by_model: { "gpt-5.4": action },
        }), { status: 200, headers: { "Content-Type": "application/json" } });
      });

    renderInputArea(true, "active work");
    await act(async () => {});
    expect(screen.getByTestId("send-btn").textContent).toBe("Interrupt");

    fireEvent.click(document.querySelector(".active-action-picker-trigger")!);
    await act(async () => {
      fireEvent.click(document.querySelector(".active-action-picker-menu button")!);
    });

    expect(fetchMock).toHaveBeenLastCalledWith(
      "/api/user-prefs",
      expect.objectContaining({
        method: "PATCH",
        body: JSON.stringify({
          composer_active_action_by_model: { "gpt-5.4": "steer" },
        }),
      }),
    );
    expect(screen.getByTestId("send-btn").textContent).toBe("Steer");
  });

  it("shows one consolidated attachment action", () => {
    renderInputArea(true, "active work");

    fireEvent.click(screen.getByRole("button", { name: "More actions" }));

    expect(screen.getByRole("button", { name: /Attach$/ })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Attach image" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Attach file" })).toBeNull();
  });

  it("edits the same prompt draft from the focused writing modal", () => {
    const onDraftChange = vi.fn();
    renderInputArea(false, "initial draft", { onDraftChange, isStreaming: false });

    fireEvent.click(screen.getByRole("button", { name: "Focus writing" }));

    const modal = screen.getByRole("dialog", { name: "Focus writing" });
    const focusedEditor = within(modal).getByTestId("composer-focus-textarea");
    expect((focusedEditor as HTMLTextAreaElement).value).toBe("initial draft");

    fireEvent.change(focusedEditor, { target: { value: "expanded draft" } });

    expect(onDraftChange).toHaveBeenLastCalledWith("expanded draft");
    expect((screen.getByTestId("input-textarea") as HTMLTextAreaElement).value).toBe(
      "expanded draft",
    );
  });

  it("sends the focused writing modal draft through the primary action", async () => {
    const onSend = vi.fn(() => true);
    renderInputArea(false, "expanded draft", { isStreaming: false, onSend });

    fireEvent.click(screen.getByRole("button", { name: "Focus writing" }));

    await act(async () => {
      fireEvent.click(screen.getByTestId("composer-focus-send-btn"));
    });

    expect(onSend).toHaveBeenCalledWith("expanded draft", [], []);
    expect(screen.queryByRole("dialog", { name: "Focus writing" })).toBeNull();
    expect((screen.getByTestId("input-textarea") as HTMLTextAreaElement).value).toBe("");
  });

  it("moves active actions into overflow by priority as composer space shrinks", () => {
    setViewportWidth(390);
    renderInputArea(true, "active work", { onStop: vi.fn() });

    expect(screen.queryByTestId("send-btn")).toBeNull();
    expect(screen.queryByTestId("stop-btn")).toBeNull();
    expect(screen.queryByTestId("composer-focus-btn")).toBeNull();
    expect(screen.getByTestId("queue-btn")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "More actions" }));
    expect(screen.getByTestId("active-action-overflow-btn").textContent).toBe("Steer");
    expect(screen.getByTestId("alternate-action-overflow-btn").textContent).toBe("Interrupt");
    expect(screen.getByTestId("stop-overflow-btn").textContent).toBe("Stop");
    expect(screen.getByTestId("composer-focus-menu-btn")).toBeTruthy();
  });

  it("keeps only the overflow trigger beside the composer when there is no action room", () => {
    setViewportWidth(280);
    renderInputArea(true, "active work", { onStop: vi.fn() });

    const row = screen.getByTestId("input-textarea").closest(".input-row")!;
    expect(row.querySelectorAll(":scope > button, :scope > .active-action-picker")).toHaveLength(0);
    expect(row.querySelectorAll(":scope > .input-overflow-wrapper")).toHaveLength(1);

    fireEvent.click(screen.getByRole("button", { name: "More actions" }));
    expect(screen.getByTestId("queue-btn")).toBeTruthy();
    expect(screen.getByTestId("stop-overflow-btn")).toBeTruthy();
  });

  it("uses Steer as the primary active Codex action", async () => {
    const { onSend, onSteer } = renderInputArea(true, "active work");

    await act(async () => {
      fireEvent.click(screen.getByTestId("send-btn"));
    });

    expect(onSteer).toHaveBeenCalledTimes(1);
    expect(onSend).toHaveBeenCalledTimes(0);
  });

  it("uses desktop Enter to queue while streaming", async () => {
    setViewportWidth(1280);
    const { onSend, onSteer } = renderInputArea(true, "active work");
    const input = screen.getByTestId("input-textarea");

    await act(async () => {
      fireEvent.keyDown(input, { key: "Enter" });
    });

    expect(onSend).toHaveBeenCalledTimes(1);
    expect(onSteer).toHaveBeenCalledTimes(0);
  });

  it("uses empty desktop Enter to steer the queued prompt for steerable providers", async () => {
    setViewportWidth(1280);
    const { onPromoteQueued, onSteerQueued } = renderInputArea(true);
    const input = screen.getByTestId("input-textarea");

    await act(async () => {
      fireEvent.keyDown(input, { key: "Enter" });
    });

    expect(onSteerQueued).toHaveBeenCalledTimes(1);
    expect(onPromoteQueued).toHaveBeenCalledTimes(0);
  });

  it("uses empty desktop Enter to interrupt the queued prompt when steering is unavailable", async () => {
    setViewportWidth(1280);
    const { onPromoteQueued, onSteerQueued } = renderInputArea(false);
    const input = screen.getByTestId("input-textarea");

    await act(async () => {
      fireEvent.keyDown(input, { key: "Enter" });
    });

    expect(onPromoteQueued).toHaveBeenCalledTimes(1);
    expect(onSteerQueued).toHaveBeenCalledTimes(0);
  });

  it("uses the explicit Queue button for active Codex queueing", async () => {
    const { onSend, onSteer } = renderInputArea(true, "active work");

    await act(async () => {
      fireEvent.click(screen.getByTestId("queue-btn"));
    });

    expect(onSend).toHaveBeenCalledTimes(1);
    expect(onSteer).toHaveBeenCalledTimes(0);
  });

  it("does not submit Steer twice on rapid double click", async () => {
    let resolveSteer!: (accepted: boolean) => void;
    const onSteer = vi.fn(
      () => new Promise<boolean>((resolve) => { resolveSteer = resolve; }),
    );
    renderInputArea(true, "active work", { onSteer });

    await act(async () => {
      fireEvent.click(screen.getByTestId("send-btn"));
      fireEvent.click(screen.getByTestId("send-btn"));
      resolveSteer(true);
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(onSteer).toHaveBeenCalledTimes(1);
  });

  it("keeps queued Steer and Interrupt available on mobile", () => {
    // Mobile defaults the queue list to collapsed; expand it for this test.
    window.localStorage.setItem("better-agent-queued-list-collapsed", "false");
    setViewportWidth(390);
    const { onPromoteQueued, onSteerQueued, onCancelQueued } = renderInputArea(true);

    const banner = screen.getByTestId("queued-prompt-banner");
    expect(within(banner).getByRole("button", { name: "Steer" })).toBeTruthy();
    expect(within(banner).getByRole("button", { name: "⚡ Interrupt" })).toBeTruthy();

    fireEvent.click(within(banner).getByRole("button", { name: "More queued actions" }));
    fireEvent.click(within(banner).getByRole("button", { name: "Cancel" }));
    expect(onCancelQueued).toHaveBeenCalledTimes(1);

    fireEvent.click(within(banner).getByRole("button", { name: "Steer" }));
    expect(onSteerQueued).toHaveBeenCalledTimes(1);

    fireEvent.click(within(banner).getByRole("button", { name: "⚡ Interrupt" }));
    expect(onPromoteQueued).toHaveBeenCalledTimes(1);
  });

  it("can minimize and expand the queued prompt banner", () => {
    renderInputArea(true);

    fireEvent.click(screen.getByRole("button", { name: "Minimize queued prompt" }));

    const minimized = screen.getByTestId("queued-prompt-banner");
    expect(minimized.getAttribute("data-minimized")).toBe("true");
    expect(within(minimized).getByRole("button", { name: "Expand queued prompt" })).toBeTruthy();
    expect(within(minimized).getByText("queued work")).toBeTruthy();
    expect(within(minimized).getByRole("button", { name: "Steer" })).toBeTruthy();
    expect(within(minimized).getByRole("button", { name: "⚡ Interrupt" })).toBeTruthy();
    expect(within(minimized).getByRole("button", { name: "Cancel" })).toBeTruthy();

    fireEvent.click(within(minimized).getByRole("button", { name: "Expand queued prompt" }));

    const expanded = screen.getByTestId("queued-prompt-banner");
    expect(expanded.getAttribute("data-minimized")).toBeNull();
    expect(within(expanded).getByRole("button", { name: "Minimize queued prompt" })).toBeTruthy();
  });

  it("persists the queued prompt minimized preference", () => {
    renderInputArea(false);
    fireEvent.click(screen.getByRole("button", { name: "Minimize queued prompt" }));
    expect(localStorage.getItem("better-agent-queued-prompt-minimized")).toBe("true");

    cleanup();
    renderInputArea(false);

    const minimized = screen.getByTestId("queued-prompt-banner");
    expect(minimized.getAttribute("data-minimized")).toBe("true");
    expect(within(minimized).getByRole("button", { name: "Expand queued prompt" })).toBeTruthy();
  });

  it("renders queued tags and summarizes attachments while minimized", () => {
    renderInputArea(true, "", {
      queuedPrompts: [{
        id: "q1",
        preview: '<inline-tags><c file="a.ts" range="1-2">check this</c></inline-tags>\n\nqueued work',
        images: [{ dataUrl: "data:image/png;base64,aaa", base64: "aaa", mediaType: "image/png" }],
        files: [{ name: "notes.txt", base64: "bbb", mediaType: "text/plain", size: 12 }],
      }],
    });

    fireEvent.click(screen.getByRole("button", { name: "Minimize queued prompt" }));

    const minimized = screen.getByTestId("queued-prompt-banner");
    expect(within(minimized).getByText("queued work")).toBeTruthy();
    expect(within(minimized).getByText("check this")).toBeTruthy();
    expect(within(minimized).getByText("a.ts:1-2")).toBeTruthy();
    expect(screen.queryByText("notes.txt")).toBeNull();
    expect(screen.getByTestId("queued-minimized-summary").textContent).toBe(
      "1 image · 1 file",
    );
  });

  it("renders comment-only inline tags as visible queued cards on desktop", () => {
    setViewportWidth(1280);
    renderInputArea(true, "", {
      queuedPrompts: [{
        id: "q1",
        preview: "<inline-tags><comment>Verify card rendering on desktop</comment><comment>Confirm comment cards stay visible and summarized</comment></inline-tags> Remaining user text after the comment tags.",
      }],
    });

    const banner = screen.getByTestId("queued-prompt-banner");
    expect(within(banner).getByText("Verify card rendering on desktop")).toBeTruthy();
    expect(within(banner).getByText("Confirm comment cards stay visible and summarized")).toBeTruthy();
    expect(within(banner).getByText("Remaining user text after the comment tags.")).toBeTruthy();
    expect(banner.textContent).not.toContain("<inline-tags>");
    expect(banner.classList.contains("has-tags")).toBe(true);

    fireEvent.click(within(banner).getByRole("button", { name: "Minimize queued prompt" }));

    const minimized = screen.getByTestId("queued-prompt-banner");
    expect(within(minimized).getByText("Verify card rendering on desktop")).toBeTruthy();
    expect(within(minimized).getByText("Confirm comment cards stay visible and summarized")).toBeTruthy();
    expect(within(minimized).getByText("Remaining user text after the comment tags.")).toBeTruthy();
    expect(minimized.getAttribute("data-minimized")).toBe("true");
  });

  it("renders mixed selected-text and plain inline comment cards readably", () => {
    setViewportWidth(1280);
    renderInputArea(true, "", {
      queuedPrompts: [{
        id: "q1",
        preview: '<inline-tags>\n<c file="src/app.tsx" range="10:1-10:24"><sel>export const Foo</sel>Verify this card renders readably</c>\n<c>Second comment — should appear as its own card</c>\n</inline-tags>\nMain user prompt text after the comment envelope.',
      }],
    });

    const banner = screen.getByTestId("queued-prompt-banner");
    expect(within(banner).getByText("src/app.tsx:10:1-10:24")).toBeTruthy();
    expect(within(banner).getByText("export const Foo")).toBeTruthy();
    expect(within(banner).getByText("Verify this card renders readably")).toBeTruthy();
    expect(within(banner).getByText("Second comment — should appear as its own card")).toBeTruthy();
    expect(within(banner).getByText("Main user prompt text after the comment envelope.")).toBeTruthy();
    expect(banner.textContent).not.toContain("<inline-tags>");
  });

  it("opens queued prompt editing from the queued item click", () => {
    const onQueuedTextEdit = vi.fn();
    renderInputArea(true, "", { onQueuedTextEdit });

    expect(screen.queryByRole("button", { name: "Edit queued prompt" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "queued work" }));

    const editor = screen.getByDisplayValue("queued work");
    fireEvent.change(editor, { target: { value: "edited queued work" } });
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
    expect(onQueuedTextEdit).toHaveBeenCalledWith("edited queued work", "q1");
  });

  it("uses the mobile-safe queued editing layout", () => {
    // Mobile defaults the queue list to collapsed; expand it for this test.
    window.localStorage.setItem("better-agent-queued-list-collapsed", "false");
    setViewportWidth(390);
    renderInputArea(true, "", { onQueuedTextEdit: vi.fn() });

    fireEvent.click(screen.getByRole("button", { name: "queued work" }));

    const modal = screen.getByRole("dialog", { name: "Edit queued prompt" });
    expect(modal.querySelector(".queued-edit-modal-header")).toBeTruthy();
    expect(modal.querySelector(".queued-prompt-edit-input")).toBeTruthy();
    expect(modal.querySelector(".queued-prompt-actions")).toBeTruthy();
  });
});
