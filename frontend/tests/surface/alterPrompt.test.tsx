// A2 restoration: legacy `MessageBubble.tsx`'s inline "Alter" edit-and-
// resend affordance (`onAlterTurnMessage`, gated to `g.isLatest && !pending`
// in Chat.tsx) had no native equivalent — `frontend/src/surface/` had zero
// grep hits for `Alter`/`alterTurn`, even though the backend command port
// (`backend/surface_commands.py`'s `send_prompt`) and wire contract
// (`backend/surface_contract/nodes.py`'s `SendMode.ALTER = "alter"`) both
// already support it end to end. Restored by threading `SendModeWire`'s
// new `"alter"` member through the SAME generic `SurfaceStore.sendPrompt`
// every other send uses (TurnView.tsx wires it, gated to the session's
// latest non-provisional turn), and a new inline editor in
// `nodes/TypedPrompt.tsx` (`AlterTriggerButton`/`AlterEditor`) reusing
// legacy's own `.message-header-actions`/`.alter-user-message-*` CSS.

import { fireEvent } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import "../../src/i18n";
import { renderApp } from "../harness";
import { makeSession } from "../fixtures";
import { turnNode, compactTurn, typedPromptNode } from "./fixtures";

// `fireEvent.change`, not a raw `el.value = ...; dispatchEvent(new
// Event("input"))` — the textarea is a REACT-CONTROLLED input
// (`value={draft}`), and a bare DOM value mutation bypasses React's
// internal value tracker, so `onChange` never fires and `draft` never
// updates. `fireEvent.change` goes through React's synthetic event system
// correctly (unlike the plain-input pattern `approvals.test.ts` uses
// elsewhere in this suite, which works there only because that legacy
// input isn't a controlled component).
function setTextareaValue(el: HTMLTextAreaElement, value: string): void {
  fireEvent.change(el, { target: { value } });
}

describe("native surface — Alter (edit-and-resend)", () => {
  it("the Alter trigger appears on the latest turn's prompt and edits+resends via send_mode=alter", async () => {
    localStorage.setItem("ba.surface_native", "1");
    const session = makeSession({ id: "sess-alter", messages: [] });
    const h = await renderApp({
      seed: { sessions: [session] },
      configureBackend: (backend) => {
        backend.seedSurface("sess-alter", {
          turns: [compactTurn(turnNode("t1"), typedPromptNode("t1", { text: "original prompt" }), [])],
        });
      },
    });
    await h.selectSession("sess-alter");
    await h.waitFor(() => h.$('[data-testid="surface-typed-prompt"]') !== null);

    const prompt = h.$('[data-testid="surface-typed-prompt"]')!;
    const trigger = prompt.querySelector(".alter-user-message-btn") as HTMLButtonElement | null;
    expect(trigger).not.toBeNull();

    await h.click(".alter-user-message-btn");
    const editor = h.$('[data-testid="surface-prompt-alter-editor"]');
    expect(editor).not.toBeNull();

    const textarea = editor!.querySelector(".alter-user-message-textarea") as HTMLTextAreaElement;
    setTextareaValue(textarea, "corrected prompt");
    await h.click(".alter-user-message-save");

    await h.waitFor(() =>
      h.outbound.some(
        (f) =>
          (f as { intent?: { kind?: string; send_mode?: string; text?: string } }).intent?.kind ===
            "send_prompt" &&
          (f as { intent?: { send_mode?: string } }).intent?.send_mode === "alter",
      ),
    );
    const sent = h.outbound.find(
      (f) => (f as { intent?: { kind?: string } }).intent?.kind === "send_prompt",
    ) as { intent: { text: string; send_mode: string } };
    expect(sent.intent.text).toBe("corrected prompt");
    expect(sent.intent.send_mode).toBe("alter");

    // The editor closes and the composer-echo (provisional turn) renders.
    expect(h.$('[data-testid="surface-prompt-alter-editor"]')).toBeNull();
    h.unmount();
  });

  it("Cancel discards the draft without sending", async () => {
    localStorage.setItem("ba.surface_native", "1");
    const session = makeSession({ id: "sess-alter-cancel", messages: [] });
    const h = await renderApp({
      seed: { sessions: [session] },
      configureBackend: (backend) => {
        backend.seedSurface("sess-alter-cancel", {
          turns: [compactTurn(turnNode("t1"), typedPromptNode("t1", { text: "original prompt" }), [])],
        });
      },
    });
    await h.selectSession("sess-alter-cancel");
    await h.waitFor(() => h.$('[data-testid="surface-typed-prompt"]') !== null);

    await h.click(".alter-user-message-btn");
    const textarea = h.$(".alter-user-message-textarea") as HTMLTextAreaElement;
    setTextareaValue(textarea, "a draft nobody should see sent");
    await h.click(".alter-user-message-cancel");

    expect(h.$('[data-testid="surface-prompt-alter-editor"]')).toBeNull();
    expect(h.$('[data-testid="surface-typed-prompt"]')?.textContent).toContain("original prompt");
    expect(
      h.outbound.some((f) => (f as { intent?: { kind?: string } }).intent?.kind === "send_prompt"),
    ).toBe(false);
    h.unmount();
  });

  it("no Alter trigger on an earlier (non-latest) turn", async () => {
    localStorage.setItem("ba.surface_native", "1");
    const session = makeSession({ id: "sess-alter-not-latest", messages: [] });
    const h = await renderApp({
      seed: { sessions: [session] },
      configureBackend: (backend) => {
        backend.seedSurface("sess-alter-not-latest", {
          turns: [
            compactTurn(turnNode("t1"), typedPromptNode("t1", { text: "first" }), []),
            compactTurn(turnNode("t2"), typedPromptNode("t2", { text: "second" }), []),
          ],
        });
      },
    });
    await h.selectSession("sess-alter-not-latest");
    await h.waitFor(() => h.$$('[data-testid="surface-typed-prompt"]').length === 2);

    const turns = h.$$('[data-testid="surface-turn"]');
    const earlier = turns.find((t) => t.getAttribute("data-turn-id") === "t1")!;
    const latest = turns.find((t) => t.getAttribute("data-turn-id") === "t2")!;
    expect(earlier.querySelector(".alter-user-message-btn")).toBeNull();
    expect(latest.querySelector(".alter-user-message-btn")).not.toBeNull();
    h.unmount();
  });
});
