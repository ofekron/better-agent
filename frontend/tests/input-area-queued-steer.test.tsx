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
      onCancelQueued={onCancelQueued}
      {...extra}
    />,
  );
  return { ...result, onSend, onSteer, onInterrupt, onPromoteQueued, onCancelQueued };
}

describe("InputArea queued prompt promote action", () => {
  it("keeps queued prompts separate from draft steering when the provider supports steering", () => {
    const { onPromoteQueued } = renderInputArea(true);

    const banner = screen.getByTestId("queued-prompt-banner");
    const interrupt = within(banner).getByRole("button", { name: "⚡ Interrupt" });
    expect(within(banner).queryByRole("button", { name: "Steer" })).toBeNull();
    expect(interrupt.getAttribute("title")).toBe(
      "Cancel current turn and send this prompt immediately",
    );
    expect(interrupt.classList.contains("interrupt")).toBe(true);

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

  it("uses Steer for desktop Enter while streaming", async () => {
    setViewportWidth(1280);
    const { onSend, onSteer } = renderInputArea(true, "active work");
    const input = screen.getByTestId("input-textarea");

    await act(async () => {
      fireEvent.keyDown(input, { key: "Enter" });
    });

    expect(onSteer).toHaveBeenCalledTimes(1);
    expect(onSend).toHaveBeenCalledTimes(0);
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

  it("keeps queued Interrupt primary and leaves queued Steer unavailable on mobile", () => {
    setViewportWidth(390);
    const { onPromoteQueued, onCancelQueued } = renderInputArea(true);

    const banner = screen.getByTestId("queued-prompt-banner");
    expect(within(banner).getByRole("button", { name: "⚡ Interrupt" })).toBeTruthy();
    expect(within(banner).queryByRole("button", { name: "Steer" })).toBeNull();

    fireEvent.click(within(banner).getByRole("button", { name: "More queued actions" }));
    expect(within(banner).queryByRole("button", { name: "Steer" })).toBeNull();
    fireEvent.click(within(banner).getByRole("button", { name: "Cancel" }));
    expect(onCancelQueued).toHaveBeenCalledTimes(1);

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
    expect(within(minimized).getByRole("button", { name: "⚡ Interrupt" })).toBeTruthy();
    expect(within(minimized).queryByRole("button", { name: "Steer" })).toBeNull();
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

  it("summarizes hidden queued comments and attachments while minimized", () => {
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
    expect(screen.queryByText("check this")).toBeNull();
    expect(screen.queryByText("notes.txt")).toBeNull();
    expect(screen.getByTestId("queued-minimized-summary").textContent).toBe(
      "1 comment · 1 image · 1 file",
    );
  });
});
