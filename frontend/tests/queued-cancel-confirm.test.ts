import { describe, it, expect } from "vitest";
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

const I18N_DIR = join(__dirname, "../src/i18n");
const APP = readFileSync(join(__dirname, "../src/App.tsx"), "utf-8");

const CONFIRM_KEYS = [
  "queued.cancelConfirmTitle_one",
  "queued.cancelConfirmTitle_other",
  "queued.cancelConfirmMessage_one",
  "queued.cancelConfirmMessage_other",
  "queued.cancelConfirmAction_one",
  "queued.cancelConfirmAction_other",
  "queued.cancelConfirmKeep",
];

function locales(): string[] {
  return readdirSync(I18N_DIR).filter((f) => f.endsWith(".json"));
}

describe("queued prompt cancel confirmation", () => {
  it("routes the queued-cancel entry point through a confirmation, not straight to the socket", () => {
    const handler = APP.slice(APP.indexOf("const handleCancelQueued"));
    const body = handler.slice(0, handler.indexOf("], ["));
    // The user-facing handler must only stage a pending confirmation. Firing
    // sendCancelQueued here would permanently discard prompt text with no undo.
    expect(body).toContain("setQueuedCancelPending");
    expect(body).not.toContain("sendCancelQueued");
  });

  it("only discards after the confirm callback runs", () => {
    const confirm = APP.slice(APP.indexOf("const confirmCancelQueued"));
    expect(confirm.slice(0, confirm.indexOf("], ["))).toContain("performCancelQueued");
  });

  it("declares the confirmation keys in every locale", () => {
    for (const file of locales()) {
      const data = JSON.parse(readFileSync(join(I18N_DIR, file), "utf-8")) as Record<string, string>;
      for (const key of CONFIRM_KEYS) {
        expect(data[key], `${file} missing ${key}`).toBeTruthy();
      }
      expect(data["queued.cancelConfirmTitle_other"]).toContain("{{count}}");
      expect(data["queued.cancelConfirmMessage_one"]).toContain("{{preview}}");
      expect(data["queued.cancelConfirmMessage_other"]).toContain("{{count}}");
      expect(data["queued.cancelConfirmMessage_other"]).toContain("{{preview}}");
    }
  });
});
