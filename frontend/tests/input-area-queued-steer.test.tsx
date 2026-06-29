import { afterEach, describe, expect, it, vi } from "vitest";
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
  setViewportWidth(1024);
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
      draft={draft}
      onDraftChange={vi.fn()}
      queuedPrompt={{ id: "q1", preview: "queued work" }}
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
    expect(onSteerQueued).toHaveBeenCalledTimes(1);
    expect(onPromoteQueued).toHaveBeenCalledTimes(0);

    fireEvent.click(interrupt);
    expect(onPromoteQueued).toHaveBeenCalledTimes(1);
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

  it("shows separate active Queue, Steer, and Interrupt buttons while streaming", () => {
    renderInputArea(true, "active work");

    expect(screen.getByTestId("send-btn").textContent).toBe("Steer");
    expect(screen.getByTestId("queue-btn").textContent).toBe("Queue");
    expect(screen.getByTestId("interrupt-btn").textContent).toBe("Interrupt");
  });

  it("shows one consolidated attachment action", () => {
    renderInputArea(true, "active work");

    fireEvent.click(screen.getByRole("button", { name: "More actions" }));

    expect(screen.getByRole("button", { name: /Attach$/ })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Attach image" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Attach file" })).toBeNull();
  });

  it("moves active Steer and Interrupt into the prompt overflow menu on mobile", () => {
    setViewportWidth(390);
    const firstStop = vi.fn();
    const first = renderInputArea(true, "active work", { onStop: firstStop });

    expect(screen.getByTestId("send-btn").textContent).toBe("Steer");
    expect(screen.queryByTestId("queue-btn")).toBeNull();
    expect(screen.queryByTestId("interrupt-btn")).toBeNull();
    expect(screen.queryByTestId("stop-btn")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "More actions" }));
    fireEvent.click(screen.getByTestId("queue-btn"));
    expect(first.onSend).toHaveBeenCalledTimes(1);
    expect(first.onSteer).toHaveBeenCalledTimes(0);

    cleanup();
    setViewportWidth(390);
    const secondStop = vi.fn();
    const second = renderInputArea(true, "active work", { onStop: secondStop });

    fireEvent.click(screen.getByRole("button", { name: "More actions" }));
    fireEvent.click(screen.getByTestId("interrupt-btn"));
    expect(second.onInterrupt).toHaveBeenCalledTimes(1);

    cleanup();
    setViewportWidth(390);
    const onStop = vi.fn();
    renderInputArea(true, "active work", { onStop });

    fireEvent.click(screen.getByRole("button", { name: "More actions" }));
    fireEvent.click(screen.getByTestId("stop-btn"));
    expect(onStop).toHaveBeenCalledTimes(1);
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
    const onSteer = vi.fn(
      () => new Promise<boolean>((resolve) => setTimeout(() => resolve(true), 10)),
    );
    renderInputArea(true, "active work", { onSteer });

    await act(async () => {
      fireEvent.click(screen.getByTestId("send-btn"));
      fireEvent.click(screen.getByTestId("send-btn"));
      await new Promise((resolve) => setTimeout(resolve, 20));
    });

    expect(onSteer).toHaveBeenCalledTimes(1);
  });

  it("keeps queued Steer and Interrupt available on mobile", () => {
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
    expect(within(minimized).queryByRole("button", { name: "Cancel" })).toBeNull();

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
      queuedPrompt: {
        id: "q1",
        preview: '<inline-tags><c file="a.ts" range="1-2">check this</c></inline-tags>\n\nqueued work',
        images: [{ dataUrl: "data:image/png;base64,aaa", base64: "aaa", mediaType: "image/png" }],
        files: [{ name: "notes.txt", base64: "bbb", mediaType: "text/plain", size: 12 }],
      },
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
      queuedPrompt: {
        id: "q1",
        preview: "<inline-tags><comment>Verify card rendering on desktop</comment><comment>Confirm comment cards stay visible and summarized</comment></inline-tags> Remaining user text after the comment tags.",
      },
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
      queuedPrompt: {
        id: "q1",
        preview: '<inline-tags>\n<c file="src/app.tsx" range="10:1-10:24"><sel>export const Foo</sel>Verify this card renders readably</c>\n<c>Second comment — should appear as its own card</c>\n</inline-tags>\nMain user prompt text after the comment envelope.',
      },
    });

    const banner = screen.getByTestId("queued-prompt-banner");
    expect(within(banner).getByText("src/app.tsx:10:1-10:24")).toBeTruthy();
    expect(within(banner).getByText("export const Foo")).toBeTruthy();
    expect(within(banner).getByText("Verify this card renders readably")).toBeTruthy();
    expect(within(banner).getByText("Second comment — should appear as its own card")).toBeTruthy();
    expect(within(banner).getByText("Main user prompt text after the comment envelope.")).toBeTruthy();
    expect(banner.textContent).not.toContain("<inline-tags>");
  });

  it("opens queued prompt editing from the explicit edit button", () => {
    const onQueuedTextEdit = vi.fn();
    renderInputArea(true, "", { onQueuedTextEdit });

    fireEvent.click(screen.getByRole("button", { name: "Edit queued prompt" }));

    const editor = screen.getByDisplayValue("queued work");
    fireEvent.change(editor, { target: { value: "edited queued work" } });
    fireEvent.blur(editor);
    expect(onQueuedTextEdit).toHaveBeenCalledWith("edited queued work");
  });

  it("uses the mobile-safe queued editing layout", () => {
    setViewportWidth(390);
    renderInputArea(true, "", { onQueuedTextEdit: vi.fn() });

    fireEvent.click(screen.getByRole("button", { name: "Edit queued prompt" }));

    const banner = screen.getByTestId("queued-prompt-banner");
    expect(banner.classList.contains("is-editing")).toBe(true);
    expect(banner.querySelector(".queued-prompt-header")).toBeTruthy();
    expect(banner.querySelector(".queued-prompt-edit-input")).toBeTruthy();
    expect(banner.querySelector(".queued-prompt-actions")).toBeTruthy();
  });
});
