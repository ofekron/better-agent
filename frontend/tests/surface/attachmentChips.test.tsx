// A1 restoration: legacy `MessageBubble.tsx`'s `UserImages` fetched each
// attachment and rendered a real `<img class="message-image">` thumbnail
// inside a click-to-open `ImageLightboxGallery`; the native
// `AttachmentChips` leaf (src/surface/leaf/AttachmentChips.tsx) used to
// render filename-only text chips with no `<img>`, no lightbox. Covers the
// leaf directly (any media_type split) and its wiring into TypedPromptView
// (the second call site, SteeringMessageView, is covered by
// tests/surface/steeringMessage.test.tsx).
//
// Thumbnails use `alt=""` (decorative — the real accessible label lives on
// the wrapping button's `aria-label`, matching legacy's own `UserImages`
// verbatim), so an empty-alt `<img>` has NO implicit ARIA "img" role —
// assertions below select `.message-image`/`.message-image-open` by CSS
// class rather than `getByRole("img", ...)`. No jest-dom matcher
// (`toHaveAttribute`) is registered anywhere in this suite's setup —
// plain `getAttribute()` reads are used instead, same as every other test
// file in this repo.
//
// AttachmentChips lazy-loads components/ImageLightboxGallery.tsx (pulls in
// framer-motion) — the first dynamic import of that module in a test run
// can outrun the default 1000ms `waitFor` timeout on a cold transform
// cache, same caveat tests/surface/render.test.tsx documents for
// MessageBox; every image-thumbnail assertion below raises its own wait
// window accordingly.

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import "../../src/i18n";
import { AttachmentChips } from "../../src/surface/leaf/AttachmentChips";
import { TypedPromptView } from "../../src/surface/nodes/TypedPrompt";
import { node, resetSeq } from "./fixtures";

resetSeq();

const COLD_IMPORT_TIMEOUT = 25000;

describe("AttachmentChips", () => {
  it(
    "renders an image attachment as an <img> thumbnail via attachmentUrl(), not a filename chip",
    async () => {
      const { container } = render(
        <AttachmentChips
          sessionId="sess-1"
          attachments={[{ name: "screenshot.png", media_type: "image/png", ref: "img-ref", size: null }]}
        />,
      );
      await waitFor(() => expect(container.querySelector("img.message-image")).not.toBeNull(), {
        timeout: COLD_IMPORT_TIMEOUT,
      });
      const img = container.querySelector("img.message-image")!;
      expect(img.getAttribute("src")).toContain("/sessions/sess-1/attachments/img-ref");
      expect(screen.queryByText("screenshot.png")).toBeNull();
    },
    30000,
  );

  it(
    "clicking the thumbnail opens the lightbox overlay",
    async () => {
      const { container } = render(
        <AttachmentChips
          sessionId="sess-1"
          attachments={[{ name: "screenshot.png", media_type: "image/png", ref: "img-ref", size: null }]}
        />,
      );
      await waitFor(() => expect(container.querySelector(".message-image-open")).not.toBeNull(), {
        timeout: COLD_IMPORT_TIMEOUT,
      });
      fireEvent.click(container.querySelector(".message-image-open")!);
      expect(container.querySelector(".image-lightbox-overlay")).not.toBeNull();
    },
    30000,
  );

  it(
    "renders multiple image attachments each as their own thumbnail",
    async () => {
      const { container } = render(
        <AttachmentChips
          sessionId="sess-1"
          attachments={[
            { name: "a.png", media_type: "image/png", ref: "ref-a", size: null },
            { name: "b.jpg", media_type: "image/jpeg", ref: "ref-b", size: null },
          ]}
        />,
      );
      await waitFor(
        () => expect(container.querySelectorAll("img.message-image")).toHaveLength(2),
        { timeout: COLD_IMPORT_TIMEOUT },
      );
    },
    30000,
  );

  it("renders a non-image attachment as a name+size file badge", () => {
    const { container } = render(
      <AttachmentChips
        sessionId="sess-1"
        attachments={[{ name: "report.pdf", media_type: "application/pdf", ref: "pdf-ref", size: 5242880 }]}
      />,
    );
    expect(screen.getByText("report.pdf")).toBeTruthy();
    expect(screen.getByText("5.0 MB")).toBeTruthy();
    expect(container.querySelector("img")).toBeNull();
  });

  it(
    "mixed attachments render images and files side by side",
    async () => {
      const { container } = render(
        <AttachmentChips
          sessionId="sess-1"
          attachments={[
            { name: "screenshot.png", media_type: "image/png", ref: "img-ref", size: null },
            { name: "notes.txt", media_type: "text/plain", ref: "txt-ref", size: 512 },
          ]}
        />,
      );
      await waitFor(() => expect(container.querySelector("img.message-image")).not.toBeNull(), {
        timeout: COLD_IMPORT_TIMEOUT,
      });
      expect(container.querySelector(".message-images")).not.toBeNull();
      expect(container.querySelector(".message-files")).not.toBeNull();
    },
    30000,
  );

  it("empty attachments renders nothing", () => {
    const { container } = render(<AttachmentChips sessionId="sess-1" attachments={[]} />);
    expect(container.firstChild).toBeNull();
  });
});

describe("TypedPromptView attachments", () => {
  it(
    "threads node.surface_id through to AttachmentChips for the attachment URL",
    async () => {
      const n = node({
        node_id: "p1",
        turn_id: "t1",
        surface_id: "sess-typed",
        kind: "typed_prompt",
        payload: {
          text: "see attached",
          attachments: [{ name: "screenshot.png", media_type: "image/png", ref: "img-ref", size: null }],
          send_mode: "queue",
          origin: "user",
          source_session_ref: null,
          sent_text: null,
          intent_id: null,
        },
      });
      const { container } = render(<TypedPromptView node={n} />);
      await waitFor(() => expect(container.querySelector("img.message-image")).not.toBeNull(), {
        timeout: COLD_IMPORT_TIMEOUT,
      });
      const img = container.querySelector("img.message-image")!;
      expect(img.getAttribute("src")).toContain("/sessions/sess-typed/attachments/img-ref");
    },
    30000,
  );
});
