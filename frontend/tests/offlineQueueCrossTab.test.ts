import { describe, expect, it } from "vitest";
import type { OfflineCreateSessionEntry, OfflinePromptEntry } from "../src/hooks/useOfflineQueue";
import { deleteOfflineAction, offlineActionKey, putOfflineAction } from "../src/lib/offlineQueueStore";
import { makeSession } from "./fixtures";
import { renderApp } from "./harness";

describe("App offline queue cross-tab projection", () => {
  it("shows a terminal create failure without a fake retry action", async () => {
    const queued: OfflineCreateSessionEntry = {
      type: "create_session",
      clientId: "terminal-create",
      prompt: "must remain visible",
      session: {
        id: "11111111-1111-4111-8111-111111111111",
        name: "Deleted session",
        model: "sonnet",
        cwd: "/tmp/project",
        orchestration_mode: "native",
        provider_id: "claude",
        browser_harness_enabled: false,
        browser_harness_headless: true,
        node_id: "primary",
        created_at: "2026-07-16T00:00:00Z",
        updated_at: "2026-07-16T00:00:00Z",
        messages: [],
        folder_id: null,
      },
      failure: { errorText: "permanently deleted", kind: "terminal" },
    };
    await putOfflineAction(queued);
    const app = await renderApp();
    for (let attempt = 0; attempt < 10 && !app.$('[data-testid="offline-queue-item"]'); attempt += 1) {
      await app.flush();
    }

    const item = app.$('[data-testid="offline-queue-item"]');
    expect(item).not.toBeNull();
    expect(item?.querySelector('[role="alert"]')?.textContent).toContain("permanently deleted");
    expect(item?.querySelector('button[aria-label="Retry"]')).toBeNull();
    expect(item?.querySelector('button[aria-label="Delete queued prompt"]')).not.toBeNull();
    expect(item?.querySelector<HTMLButtonElement>('.offline-queue-preview')?.disabled).toBe(true);
    expect(app.toJSON().chat.messages).not.toContainEqual(expect.objectContaining({
      id: "terminal-create",
    }));
    app.unmount();
  });

  it("removes an offline bubble after another tab deletes its durable action", async () => {
    const queued: OfflinePromptEntry = {
      sessionId: "session-a",
      clientId: "offline-a",
      prompt: "queued in another tab",
      model: "sonnet",
      cwd: "/tmp/project",
      sendMode: "queue",
      editing: { draftPrompt: "queued in another tab" },
    };
    await putOfflineAction(queued);
    window.history.replaceState({}, "", "/s/session-a");
    const app = await renderApp({
      seed: { sessions: [makeSession({ id: "session-a", cwd: "/tmp/project" })] },
    });
    for (let attempt = 0; attempt < 10 && !app.$('[data-message-id="offline-a"]'); attempt += 1) {
      await app.flush();
    }
    expect(app.toJSON().chat.messages).toContainEqual(expect.objectContaining({
      id: "offline-a",
      status: "offline",
    }));
    expect(app.$('[data-testid="offline-queue-item"]')).not.toBeNull();

    await deleteOfflineAction(offlineActionKey(queued));
    window.dispatchEvent(new CustomEvent("better-agent-offline-queue-changed", {
      detail: "another-tab",
    }));
    for (let attempt = 0; attempt < 10 && app.$('[data-message-id="offline-a"]'); attempt += 1) {
      await app.flush();
    }

    expect(app.toJSON().chat.messages).not.toContainEqual(expect.objectContaining({ id: "offline-a" }));
    expect(app.$('[data-testid="offline-queue-item"]')).toBeNull();
    app.unmount();
  });
});
