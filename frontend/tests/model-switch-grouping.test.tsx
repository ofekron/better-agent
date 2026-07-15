import { describe, it, expect } from "vitest";
import { waitFor } from "@testing-library/react";
import { renderApp } from "./harness";
import { makeAssistantMsg, makeSession, makeUserMsg } from "./fixtures";

/**
 * A model switch is recorded against the previous turn's assistant message
 * (the only message that exists at switch time) but takes effect on the NEXT
 * turn. The banner must therefore render heading the following user prompt's
 * turn group, never trailing the finished response's group.
 */
describe("model-switch event grouping", () => {
  const modelSwitchEvent = {
    type: "model_switched" as const,
    data: {
      uuid: "model-switch-1",
      previous_provider_id: "claude",
      previous_model: "sonnet",
      provider_id: "codex",
      model: "gpt-5-codex",
      changed: ["provider_id", "model"],
    },
  };

  it("renders the switch banner preceding the next user prompt, not under the previous group", async () => {
    const session = makeSession({
      id: "sess-switch",
      messages: [
        makeUserMsg({ id: "u1", content: "first prompt" }),
        makeAssistantMsg({ id: "a1", content: "first reply", events: [modelSwitchEvent] }),
        makeUserMsg({ id: "u2", content: "second prompt" }),
        makeAssistantMsg({ id: "a2", content: "second reply" }),
      ],
    });

    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession("sess-switch");

    const group1 = h.$("#msg-u1")?.closest(".turn-group") ?? null;
    const group2 = h.$("#msg-u2")?.closest(".turn-group") ?? null;
    expect(group1).not.toBeNull();
    expect(group2).not.toBeNull();

    // Banner belongs to the SECOND group (heads the next prompt)…
    await waitFor(() => expect(h.$("#msg-u2")?.closest(".turn-group")?.querySelector('[data-testid="model-switch-preceding"]')).not.toBeNull());
    const currentGroup2 = h.$("#msg-u2")?.closest(".turn-group") ?? null;
    const currentGroup1 = h.$("#msg-u1")?.closest(".turn-group") ?? null;
    expect(currentGroup2!.querySelector(".event-model-switched")?.textContent).toContain(
      "claude / sonnet to codex / gpt-5-codex",
    );
    // …and must NOT appear under the first (finished) group.
    expect(currentGroup1!.querySelector(".event-model-switched")).toBeNull();

    h.unmount();
  });

  it("renders a trailing switch banner as a preface when no next prompt exists yet", async () => {
    const session = makeSession({
      id: "sess-switch-tail",
      messages: [
        makeUserMsg({ id: "u1", content: "only prompt" }),
        makeAssistantMsg({ id: "a1", content: "only reply", events: [modelSwitchEvent] }),
      ],
    });

    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession("sess-switch-tail");

    const group1 = h.$("#msg-u1")?.closest(".turn-group") ?? null;
    expect(group1).not.toBeNull();
    // Rendered as a trailing preface, not inline within the finished response.
    await waitFor(() => expect(h.$("#msg-u1")?.closest(".turn-group")?.querySelector('[data-testid="model-switch-trailing"]')).not.toBeNull());
    const currentGroup1 = h.$("#msg-u1")?.closest(".turn-group") ?? null;
    expect(currentGroup1!.querySelector('[data-testid="model-switch-preceding"]')).toBeNull();

    h.unmount();
  });

  it("does not render selector-change anchors as empty assistant turns", async () => {
    const session = makeSession({
      id: "sess-switch-anchor",
      provider_id: "codex",
      model: "gpt-5-codex",
      reasoning_effort: "medium",
      messages: [
        makeUserMsg({ id: "u1", content: "first prompt" }),
        makeAssistantMsg({
          id: "a1",
          content: "first reply",
          run_meta: { provider_id: "claude", model: "sonnet", reasoning_effort: "medium" },
        }),
        makeAssistantMsg({
          id: "selector-anchor",
          content: "",
          source: "selector_change",
          events: [modelSwitchEvent],
          run_meta: { provider_id: "codex", model: "gpt-5-codex", reasoning_effort: "medium" },
        }),
      ],
    });

    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession("sess-switch-anchor");

    expect(h.$("#msg-selector-anchor")).toBeNull();
    expect(h.$(".collapse-summary")?.textContent ?? "").not.toContain("No output");
    expect(h.$$(".assistant-run-meta-footer")).toHaveLength(1);
    expect(h.$("#msg-u1")?.closest(".turn-group")?.querySelector('[data-testid="model-switch-trailing"]')).not.toBeNull();

    h.unmount();
  });

  it("carries selector-change anchors forward as the next turn preface", async () => {
    const session = makeSession({
      id: "sess-switch-anchor-next",
      messages: [
        makeUserMsg({ id: "u1", content: "first prompt" }),
        makeAssistantMsg({ id: "a1", content: "first reply" }),
        makeAssistantMsg({
          id: "selector-anchor",
          content: "",
          source: "selector_change",
          events: [modelSwitchEvent],
        }),
        makeUserMsg({ id: "u2", content: "second prompt" }),
        makeAssistantMsg({ id: "a2", content: "second reply" }),
      ],
    });

    const h = await renderApp({ seed: { sessions: [session] } });
    await h.selectSession("sess-switch-anchor-next");

    const group1 = h.$("#msg-u1")?.closest(".turn-group") ?? null;
    const group2 = h.$("#msg-u2")?.closest(".turn-group") ?? null;
    expect(group1).not.toBeNull();
    expect(group2).not.toBeNull();
    expect(h.$("#msg-selector-anchor")).toBeNull();
    await waitFor(() => expect(group2!.querySelector('[data-testid="model-switch-preceding"]')).not.toBeNull());
    expect(group2!.querySelector(".event-model-switched")?.textContent).toContain(
      "claude / sonnet to codex / gpt-5-codex",
    );
    expect(group1!.querySelector(".event-model-switched")).toBeNull();

    h.unmount();
  });
});
