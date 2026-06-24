import { useRef } from "react";
import { render, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { useMessageDecorations } from "../src/hooks/useMessageDecorations";
import type { InlineTag } from "../src/types/inlineTag";

// Module-constant tags so their array identity never changes across
// re-renders — the real bug condition. In AssistantMessage the stub→full
// fetch swaps `effectiveMessage` (bumping `decorationRevision`, the
// remount key) while the filtered `tags` array keeps its identity, so
// only `revision` changes. This host reproduces that: a stable outer
// ref carries the hook, and an INNER div keyed on `revision` remounts
// when it bumps — discarding any injected highlight spans.
const TAGS: InlineTag[] = [
  { id: "tag-1", messageId: "message-1", selectedText: "selected", comment: "", timestamp: "" },
];

function Host({ revision }: { revision: number }) {
  const ref = useRef<HTMLDivElement>(null);
  useMessageDecorations(ref, { tags: TAGS, revision });
  return (
    <div ref={ref}>
      <div key={revision}>selected text</div>
    </div>
  );
}

describe("useMessageDecorations", () => {
  it("re-applies highlights after the message body remounts", async () => {
    const view = render(<Host revision={1} />);
    await waitFor(() => {
      expect(view.container.querySelector(".inline-tag-highlight")).not.toBeNull();
    });

    // Bump the revision: the inner div remounts and the injected span
    // is discarded. Without `revision` in the hook deps the highlight
    // never returns — this is the reported intermittently-missing
    // highlight on lazily-fetched (stub→full) messages.
    view.rerender(<Host revision={2} />);
    await waitFor(() => {
      expect(view.container.querySelector(".inline-tag-highlight")).not.toBeNull();
    });
  });
});
