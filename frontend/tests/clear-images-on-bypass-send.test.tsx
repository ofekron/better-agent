/**
 * Regression test — attached images must clear after a send, even when
 * the one-time bypass-permission interstitial intercepts the send.
 *
 * Bug: `App.handleSend` returned `undefined` when it popped the bypass
 * modal, so `InputArea.submitDraft` saw a falsy `sent` and never ran its
 * image/file clear (`setImages([])` / `setFiles([])`). The actual send
 * then went out via `confirmBypassAndSend → sendPrompt`, bypassing
 * submitDraft entirely. Net effect: the prompt text cleared (it's cleared
 * optimistically) but the attached image stayed in the box after a
 * successful send.
 *
 * Invariant: after a successful send through the bypass modal, both the
 * textarea AND the attached-image previews are empty.
 */
import { describe, expect, it } from "vitest";
import "../src/i18n";
import { renderApp } from "./harness";
import { makeSession } from "./fixtures";

describe("attached images clear on send through bypass modal", () => {
  it("empties image previews after Send anyway", async () => {
    // Codex bypass target: approval=never + sandbox=danger-full-access.
    const session = makeSession({
      provider_id: "codex",
      permission: { approval: "never", sandbox: "danger-full-access" },
      draft_images: [
        { dataUrl: "data:image/png;base64,QUJD", base64: "QUJD", mediaType: "image/png" },
      ],
    });
    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession(session.id);

    // The seeded draft image renders as a preview.
    expect(h.$$(".image-preview-item").length).toBe(1);

    await h.typeAndSend("look at this");

    // The bypass interstitial intercepted the first send — confirm it.
    await h.clickByText(/Send anyway/);
    await h.flush();

    // The send went out...
    const sent = h.outbound.findLast((f) => f.type === "send_message");
    expect(sent?.prompt).toBe("look at this");

    // ...and BOTH the textarea and the image previews are now empty.
    expect(h.toJSON().input.text).toBe("");
    expect(h.$$(".image-preview-item").length).toBe(0);

    h.unmount();
  });
});
