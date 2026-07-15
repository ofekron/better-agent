import { describe, expect, it } from "vitest";
import type { OfflinePromptEntry } from "../src/hooks/useOfflineQueue";
import { deleteOfflineAction, offlineActionKey, putOfflineAction } from "../src/lib/offlineQueueStore";
import { makeSession } from "./fixtures";
import { renderApp } from "./harness";

describe("App offline queue cross-tab projection", () => {
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
