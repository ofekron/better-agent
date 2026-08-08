// `steering_message` node — SteeringMessageView (src/surface/nodes/ContentLeaves.tsx).
// Round 1 ported the shell/text rendering only (tests/messagebubble-steerpromptevent.test.tsx,
// deleted). Round 2 closes the flagged gap: artificial-section-chip
// collapsing now goes through the shared `leaf/ArtificialSections.tsx`
// renderer (same parser/chips TypedPromptView uses — see
// tests/surface/promptOrigin.test.tsx and
// tests/artificialSectionChips.test.tsx for the shared-renderer coverage;
// this file just proves SteeringMessageView actually wires it in).
// Attachments: `SteeringMessagePayloadWire.attachments` (src/adapter/wire.ts)
// now carries the same `AttachmentWire[]` shape as
// `TypedPromptPayloadWire.attachments`, populated by backend
// `normalize._handle_steer_prompt` from the steer's saved images — rendered
// through the shared `leaf/AttachmentChips.tsx` component (same renderer
// TypedPromptView uses): an image attachment renders as a real `<img>`
// thumbnail (legacy `UserImages` parity, A1 restoration), everything else
// as a `.message-file-badge` name+size chip (legacy `UserFiles` parity) —
// covered below.

import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { SteeringMessageView } from "../../src/surface/nodes/ContentLeaves";
import { node, resetSeq } from "./fixtures";

resetSeq();

describe("SteeringMessageView", () => {
  it("renders the Steer label and text container", () => {
    const n = node({
      node_id: "s1",
      turn_id: "t1",
      kind: "steering_message",
      payload: { text: "hello", target: "", attachments: [] },
    });
    render(<SteeringMessageView node={n} />);
    const el = screen.getByTestId("surface-steering-message");
    expect(el.querySelector(".event-steer-label")?.textContent).toBe("Steer");
    expect(el.querySelector(".event-steer-text")?.textContent).toContain("hello");
  });

  // AttachmentChips lazy-loads components/ImageLightboxGallery.tsx (pulls
  // in framer-motion) — the first dynamic import of that module in a test
  // run can outrun the default 1000ms `waitFor` timeout on a cold
  // transform cache, same caveat tests/surface/render.test.tsx documents
  // for MessageBox. The thumbnail uses `alt=""` (decorative, matching
  // legacy's own `UserImages`), so it has NO implicit ARIA "img" role —
  // selected by CSS class instead of `getByRole`. No jest-dom matcher is
  // registered in this suite's setup — plain `getAttribute()` is used
  // instead of `toHaveAttribute`.
  it(
    "renders an image attachment as a real thumbnail, same renderer as TypedPromptView",
    async () => {
      const n = node({
        node_id: "s4",
        turn_id: "t1",
        surface_id: "sess-1",
        kind: "steering_message",
        payload: {
          text: "see attached",
          target: "",
          attachments: [{ name: "screenshot.png", media_type: "image/png", ref: "screenshot.png", size: null }],
        },
      });
      render(<SteeringMessageView node={n} />);
      const el = screen.getByTestId("surface-steering-message");
      await waitFor(() => expect(el.querySelector("img.message-image")).not.toBeNull(), { timeout: 25000 });
      const img = el.querySelector("img.message-image")!;
      expect(img.getAttribute("src")).toContain("/sessions/sess-1/attachments/screenshot.png");
      expect(el.querySelector(".message-image-open")).not.toBeNull();
      expect(el.querySelector(".message-file-badge")).toBeNull();
    },
    30000,
  );

  it("renders a non-image attachment as a name+size file badge, not a thumbnail", () => {
    const n = node({
      node_id: "s6",
      turn_id: "t1",
      surface_id: "sess-1",
      kind: "steering_message",
      payload: {
        text: "see attached",
        target: "",
        attachments: [{ name: "notes.txt", media_type: "text/plain", ref: "notes.txt", size: 2048 }],
      },
    });
    render(<SteeringMessageView node={n} />);
    const el = screen.getByTestId("surface-steering-message");
    const badge = el.querySelector(".message-file-badge");
    expect(badge?.querySelector(".message-file-name")?.textContent).toBe("notes.txt");
    expect(badge?.querySelector(".message-file-size")?.textContent).toBe("2.0 KB");
    expect(el.querySelector("img")).toBeNull();
  });

  it("no attachments renders no attachment chrome", () => {
    const n = node({
      node_id: "s5",
      turn_id: "t1",
      kind: "steering_message",
      payload: { text: "hello", target: "", attachments: [] },
    });
    render(<SteeringMessageView node={n} />);
    const el = screen.getByTestId("surface-steering-message");
    expect(el.querySelector(".message-images")).toBeNull();
    expect(el.querySelector(".message-files")).toBeNull();
  });

  it("renders an artificial-section tag as a collapsed chip, not raw XML", () => {
    const n = node({
      node_id: "s2",
      turn_id: "t1",
      kind: "steering_message",
      payload: { text: "<system-reminder>be brief</system-reminder>", target: "", attachments: [] },
    });
    render(<SteeringMessageView node={n} />);
    const el = screen.getByTestId("surface-steering-message");
    expect(el.textContent).not.toContain("<system-reminder>");
    expect(el.querySelector(".artificial-section-chip.artificial-section-system-reminder")).not.toBeNull();
    expect(el.querySelector(".artificial-section-body")).toBeNull(); // collapsed by default
  });

  it("null payload renders nothing", () => {
    const n = node({ node_id: "s3", turn_id: "t1", kind: "steering_message", payload: null });
    const { container } = render(<SteeringMessageView node={n} />);
    expect(container.firstChild).toBeNull();
  });
});
