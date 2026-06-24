import { describe, expect, it } from "vitest";
import { waitFor } from "@testing-library/react";
import { renderApp } from "./harness";
import { makeSession } from "./fixtures";

describe("capability picker session wiring", () => {
  it("sends a next-turn capability once and then clears it", async () => {
    const session = makeSession();
    const h = await renderApp({
      seed: {
        sessions: [session],
        capabilitySources: [
          {
            source_id: "project:skill:reviewer",
            source_scope: "project",
            source_label: "Project",
            source_cwd: session.cwd,
            capability: {
              capability_id: "skill-reviewer",
              name: "Reviewer",
              category: "skill",
              total_token_count: 12,
            },
            outputs: [
              {
                provider_kind: "codex",
                provider_name: "Codex",
                content_kind: "codex_skill",
                content: "Review carefully.",
              },
            ],
          },
        ],
      },
    });
    await h.selectSession(session.id);

    await h.click(".input-overflow-trigger");
    await h.click('[data-testid="add-turn-capability-btn"]');
    await waitFor(() => {
      expect(h.raw.container.textContent).toContain("Reviewer");
    });
    await h.clickByText(/Reviewer/);
    expect(h.raw.container.textContent).toContain("Reviewer");

    await h.typeAndSend("first");
    const first = h.outbound.filter((f) => f.type === "send_message").at(-1);
    expect(first).toMatchObject({
      prompt: "first",
      capability_contexts: [
        {
          source_id: "project:skill:reviewer",
          outputs: [
            {
              provider_kind: "codex",
              content: "Review carefully.",
            },
          ],
        },
      ],
    });

    await h.typeAndSend("second");
    const second = h.outbound.filter((f) => f.type === "send_message").at(-1);
    expect(String(second?.prompt ?? "")).toContain("second");
    expect(second).not.toHaveProperty("capability_contexts");
    h.unmount();
  });
});
