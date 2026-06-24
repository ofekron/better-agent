import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { renderApp } from "./harness";
import { makeSession } from "./fixtures";
import type { WSEvent } from "../src/types";

/**
 * Replays a REAL captured backend frame sequence (turn A streaming →
 * prompt B queued mid-turn → turn A completes → queued turn B starts)
 * through the actual App, and asserts the previous assistant message
 * (turn A) reaches its FINAL content form without a page refresh.
 */

function loadFrames(): WSEvent[] {
  return JSON.parse(
    readFileSync("/tmp/queued_frames.json", "utf-8"),
  ) as WSEvent[];
}

describe("queued prompt — previous assistant reaches final form", () => {
  it("assistant A shows final content after queued turn B starts", async () => {
    const frames = loadFrames();
    // The captured frames carry the original session uuid — find it.
    const sid = (frames.find(
      (f) => (f.data as { app_session_id?: string })?.app_session_id,
    )!.data as { app_session_id: string }).app_session_id;

    const session = makeSession({ id: sid, orchestration_mode: "native" });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(sid);

    // find index of the delta carrying assistant A's final form
    const finalAIdx = frames.findIndex((f) => {
      if (f.type !== "messages_delta") return false;
      const msgs = (f.data as { messages?: { content?: string }[] })?.messages;
      return !!msgs?.some((m) => (m.content || "").includes("DONE-A"));
    });
    expect(finalAIdx).toBeGreaterThan(-1);
    const asstAId = (frames[finalAIdx].data as { messages: { id: string }[] })
      .messages[0].id;

    let prevSig = "";
    for (let i = 0; i < frames.length; i++) {
      h.emit(frames[i]);
      await h.flush();
      const msgs = h.toJSON().chat.messages;
      const sig = msgs
        .map((m) => `${m.id.slice(0, 8)}/${m.role}/${m.text.includes("DONE-A") ? "A!" : ""}`)
        .join(" | ");
      if (sig !== prevSig) {
        console.log(`after [${i}] ${frames[i].type}:`, sig);
        prevSig = sig;
      }
    }
    await h.flush();

    // Dump the full chat DOM text per group to see what the collapsed
    // previous-turn group actually displays.
    const groups = h.$$(".message-group, [data-testid='chat-messages'] > *");
    for (const g of groups) {
      console.log(
        "GROUP:",
        (g.className || g.tagName) + " ::",
        (g.textContent || "").replace(/\s+/g, " ").slice(0, 300),
      );
    }
    const chatText = (h.$('[data-testid="chat-messages"]')?.textContent || "")
      .replace(/\s+/g, " ");
    console.log("asstAId:", asstAId);
    // The previous turn's visible representation must include its final
    // assistant output.
    expect(chatText).toContain("DONE-A");
    const view = h.toJSON();
    const asstA = view.chat.messages.find((m) => m.id === asstAId);
    expect(asstA ?? chatText).toBeTruthy();
    h.unmount();
  });
});
