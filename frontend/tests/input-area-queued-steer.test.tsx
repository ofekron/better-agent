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
  it("shows both Steer and Interrupt on queued prompts when the active provider supports steering", () => {
    const { onPromoteQueued, onSteerQueued } = renderInputArea(true);

    const banner = screen.getByTestId("queued-prompt-banner");
    const steer = within(banner).getByRole("button", { name: "Steer" });
    const interrupt = within(banner).getByRole("button", { name: "⚡ Interrupt" });
    expect(steer.getAttribute("title")).toBe("Send into the active Codex turn");
    expect(steer.classList.contains("steer")).toBe(true);
    expect(interrupt.getAttribute("title")).toBe(
      "Cancel current turn and send this prompt immediately",
    );
    expect(interrupt.classList.contains("interrupt")).toBe(true);

    fireEvent.click(steer);
    fireEvent.click(interrupt);
    expect(onSteerQueued).toHaveBeenCalledTimes(1);
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

  it("shows separate active Steer and Interrupt buttons while streaming", () => {
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

  it("moves active Codex alternatives into the prompt overflow menu on mobile", () => {
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
    const { onSteer } = renderInputArea(true, "active work");

    await act(async () => {
      fireEvent.click(screen.getByTestId("send-btn"));
    });

    expect(onSteer).toHaveBeenCalledTimes(1);
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

  it("keeps queued Interrupt primary and moves queued Steer into overflow on mobile", () => {
    setViewportWidth(390);
    const { onPromoteQueued, onSteerQueued, onCancelQueued } = renderInputArea(true);

    const banner = screen.getByTestId("queued-prompt-banner");
    expect(within(banner).getByRole("button", { name: "⚡ Interrupt" })).toBeTruthy();
    expect(within(banner).queryByRole("button", { name: "Steer" })).toBeNull();

    fireEvent.click(within(banner).getByRole("button", { name: "More queued actions" }));
    fireEvent.click(within(banner).getByRole("button", { name: "Steer" }));
    expect(onSteerQueued).toHaveBeenCalledTimes(1);

    fireEvent.click(within(banner).getByRole("button", { name: "⚡ Interrupt" }));
    expect(onPromoteQueued).toHaveBeenCalledTimes(1);

    fireEvent.click(within(banner).getByRole("button", { name: "More queued actions" }));
    fireEvent.click(within(banner).getByRole("button", { name: "Cancel" }));
    expect(onCancelQueued).toHaveBeenCalledTimes(1);
  });
});
