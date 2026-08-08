// Native replacement for tests/injected-prompt-label.test.tsx — TypedPromptView
// origin-label parity (round 2 gap: "no visible origin label at all" for a
// typed_prompt whose `payload.origin` is not the plain source-less "user"
// case). Seeded via `configureBackend` (not a post-renderApp seedSurface()
// call) per tests/surfaceHarness.test.tsx's header gotcha — a single
// seeded session races ChatSurfaceView's own first fetchSnapshot()
// otherwise.

import { describe, it, expect } from "vitest";
import "../../src/i18n";
import { renderApp } from "../harness";
import { makeSession } from "../fixtures";
import { turnNode, compactTurn, typedPromptNode } from "./fixtures";

describe("native surface — TypedPromptView origin labels", () => {
  it("a plain user-typed prompt (origin=user) shows the configured-username header", async () => {
    // Was "shows no origin header" — the plain-user label gap (round-2 item
    // 2's scope-blocked sub-part: userDisplayName had no path from App.tsx
    // down to ChatSurfaceView) is now closed (Chat.tsx threads it through
    // ChatSurfaceView -> TurnView -> TypedPromptView's PlainUserHeader,
    // matching legacy TurnGroupImpl's always-shown initiator header).
    // MockBackend's authed user is "test-user" (tests/harness/mockBackend.ts),
    // which App.tsx surfaces as `authedUser?.username` — the SAME source
    // legacy's own `userDisplayName` prop already used, no test-only stub.
    localStorage.setItem("ba.surface_native", "1");
    const session = makeSession({ id: "sess-origin-user", messages: [] });
    const h = await renderApp({
      seed: { sessions: [session] },
      configureBackend: (backend) => {
        backend.seedSurface("sess-origin-user", {
          turns: [compactTurn(turnNode("t1"), typedPromptNode("t1", { text: "hi" }), [])],
        });
      },
    });
    await h.selectSession("sess-origin-user");
    await h.waitFor(() => h.$('[data-testid="surface-typed-prompt"]') !== null);

    const prompt = h.$('[data-testid="surface-typed-prompt"]')!;
    expect(prompt.getAttribute("data-origin")).toBe("user");
    expect(prompt.querySelector(".message-box-label")?.textContent).toBe("test-user");
    expect(prompt.querySelector(".message-box-label.orchestration-label")).toBeNull();
    expect(prompt.textContent).toContain("hi");
    h.unmount();
  });

  it("origin=ask is labeled Ask, never rendered as a plain user prompt", async () => {
    localStorage.setItem("ba.surface_native", "1");
    const session = makeSession({ id: "sess-origin-ask", messages: [] });
    const h = await renderApp({
      seed: { sessions: [session] },
      configureBackend: (backend) => {
        backend.seedSurface("sess-origin-ask", {
          turns: [compactTurn(turnNode("t1"), typedPromptNode("t1", { text: "please help", origin: "ask" }), [])],
        });
      },
    });
    await h.selectSession("sess-origin-ask");
    await h.waitFor(() => h.$('[data-testid="surface-typed-prompt"]') !== null);

    const prompt = h.$('[data-testid="surface-typed-prompt"]')!;
    expect(prompt.getAttribute("data-origin")).toBe("ask");
    expect(prompt.querySelector(".message-box-label")?.textContent).toBe("Ask");
    h.unmount();
  });

  it("origin=ask with source_session_ref shows a FROM sender link", async () => {
    localStorage.setItem("ba.surface_native", "1");
    const session = makeSession({ id: "sess-origin-ask-from", messages: [] });
    const h = await renderApp({
      seed: { sessions: [session] },
      configureBackend: (backend) => {
        backend.seedSurface("sess-origin-ask-from", {
          turns: [
            compactTurn(
              turnNode("t1"),
              typedPromptNode("t1", { text: "please help", origin: "ask", source_session_ref: "sender-session-1234" }),
              [],
            ),
          ],
        });
      },
    });
    await h.selectSession("sess-origin-ask-from");
    await h.waitFor(() => h.$('[data-testid="surface-typed-prompt"]') !== null);

    const prompt = h.$('[data-testid="surface-typed-prompt"]')!;
    const from = prompt.querySelector(".team-message-from");
    expect(from).not.toBeNull();
    expect(from?.textContent).toContain("FROM");
    expect(from?.textContent).toContain("sender-session-1234");
    h.unmount();
  });

  it("origin=offline_sync is labeled Offline", async () => {
    localStorage.setItem("ba.surface_native", "1");
    const session = makeSession({ id: "sess-origin-offline", messages: [] });
    const h = await renderApp({
      seed: { sessions: [session] },
      configureBackend: (backend) => {
        backend.seedSurface("sess-origin-offline", {
          turns: [compactTurn(turnNode("t1"), typedPromptNode("t1", { text: "queued while offline", origin: "offline_sync" }), [])],
        });
      },
    });
    await h.selectSession("sess-origin-offline");
    await h.waitFor(() => h.$('[data-testid="surface-typed-prompt"]') !== null);

    const prompt = h.$('[data-testid="surface-typed-prompt"]')!;
    expect(prompt.querySelector(".message-box-label")?.textContent).toBe("Offline");
    h.unmount();
  });

  it("origin=queued shows the existing queued badge, no redundant origin header", async () => {
    localStorage.setItem("ba.surface_native", "1");
    const session = makeSession({ id: "sess-origin-queued", messages: [] });
    const h = await renderApp({
      seed: { sessions: [session] },
      configureBackend: (backend) => {
        backend.seedSurface("sess-origin-queued", {
          turns: [compactTurn(turnNode("t1"), typedPromptNode("t1", { text: "still queued", origin: "queued" }), [])],
        });
      },
    });
    await h.selectSession("sess-origin-queued");
    await h.waitFor(() => h.$('[data-testid="surface-typed-prompt"]') !== null);

    const prompt = h.$('[data-testid="surface-typed-prompt"]')!;
    expect(prompt.querySelector(".message-box-label")).toBeNull();
    expect(prompt.querySelector(".surface-prompt-queued")).not.toBeNull();
    h.unmount();
  });

  it("origin=supervisor renders as a collapsed chip, expands to reveal the full text", async () => {
    localStorage.setItem("ba.surface_native", "1");
    const session = makeSession({ id: "sess-origin-supervisor", messages: [] });
    const longText = "line one of the adversarial verdict prompt\nsecond line with worker output embedded";
    const h = await renderApp({
      seed: { sessions: [session] },
      configureBackend: (backend) => {
        backend.seedSurface("sess-origin-supervisor", {
          turns: [compactTurn(turnNode("t1"), typedPromptNode("t1", { text: longText, origin: "supervisor" }), [])],
        });
      },
    });
    await h.selectSession("sess-origin-supervisor");
    await h.waitFor(() => h.$('[data-testid="surface-typed-prompt"]') !== null);

    const prompt = h.$('[data-testid="surface-typed-prompt"]')!;
    expect(prompt.getAttribute("data-origin")).toBe("supervisor");
    const chip = prompt.querySelector(".synthetic-prompt-chip.synthetic-prompt-supervisor");
    expect(chip).not.toBeNull();
    expect(chip?.querySelector(".synthetic-prompt-label")?.textContent).toBe("Supervisor prompt");
    // Collapsed by default: preview only, full text not in the DOM.
    expect(chip?.querySelector(".synthetic-prompt-preview")?.textContent).toContain("line one of the adversarial verdict prompt");
    expect(prompt.textContent).not.toContain("second line with worker output embedded");

    const header = chip!.querySelector(".synthetic-prompt-header") as HTMLButtonElement;
    header.click();
    await h.flush();

    expect(chip?.querySelector(".synthetic-prompt-body")?.textContent).toContain("second line with worker output embedded");
    h.unmount();
  });

  it("sent_text divergence renders an on-demand disclosure of the actually-dispatched prompt", async () => {
    localStorage.setItem("ba.surface_native", "1");
    const session = makeSession({ id: "sess-origin-senttext", messages: [] });
    const h = await renderApp({
      seed: { sessions: [session] },
      configureBackend: (backend) => {
        backend.seedSurface("sess-origin-senttext", {
          turns: [
            compactTurn(
              turnNode("t1"),
              typedPromptNode("t1", { text: "do the thing", sent_text: "<system>wrapped</system>do the thing" }),
              [],
            ),
          ],
        });
      },
    });
    await h.selectSession("sess-origin-senttext");
    await h.waitFor(() => h.$('[data-testid="surface-typed-prompt"]') !== null);

    const disclosure = h.$('[data-testid="surface-prompt-sent-text"]')!;
    expect(disclosure).not.toBeNull();
    expect(disclosure.textContent).not.toContain("wrapped");
    (disclosure.querySelector("button") as HTMLButtonElement).click();
    await h.flush();
    expect(disclosure.textContent).toContain("wrapped");
    h.unmount();
  });

  it("sent_text identical to text renders no disclosure", async () => {
    localStorage.setItem("ba.surface_native", "1");
    const session = makeSession({ id: "sess-origin-senttext-same", messages: [] });
    const h = await renderApp({
      seed: { sessions: [session] },
      configureBackend: (backend) => {
        backend.seedSurface("sess-origin-senttext-same", {
          turns: [
            compactTurn(
              turnNode("t1"),
              typedPromptNode("t1", { text: "same text", sent_text: "same text" }),
              [],
            ),
          ],
        });
      },
    });
    await h.selectSession("sess-origin-senttext-same");
    await h.waitFor(() => h.$('[data-testid="surface-typed-prompt"]') !== null);

    expect(h.$('[data-testid="surface-prompt-sent-text"]')).toBeNull();
    h.unmount();
  });
});
