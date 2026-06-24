import type { InlineTag } from "../types/inlineTag";

const FOCUSED_CLASS = "inline-tag-highlight-focused";
const SELECTED_CLASS = "inline-tag-highlight-selected";

/** Tag id used by SelectionPopup for the live text-selection preview.
 *  Spans with this id get the strong "selected" emphasis — visibly
 *  stronger than the gentle always-on tint of persisted comments — so
 *  the user clearly sees what they are about to tag. */
export const PENDING_TAG_ID = "__pending__";

/** Tag id whose highlight is aggressively emphasized. Module-level so
 *  spans created by a later `applyTagHighlights` re-apply (e.g. after a
 *  tag edit re-renders the message) come up already focused — no timing
 *  dance between React effects and the 50ms-delayed highlight pass. */
let focusedTagId: string | null = null;

/** Emphasize the highlight spans of one tag (and de-emphasize all
 *  others). Pass null to clear. */
export function setFocusedTagHighlight(tagId: string | null): void {
  focusedTagId = tagId;
  for (const el of document.querySelectorAll(`.${FOCUSED_CLASS}`)) {
    el.classList.remove(FOCUSED_CLASS);
  }
  if (!tagId) return;
  for (const el of document.querySelectorAll(
    `.inline-tag-highlight[data-tag-id="${CSS.escape(tagId)}"]`,
  )) {
    el.classList.add(FOCUSED_CLASS);
  }
}

/** Apply highlight spans around tagged text inside a container element,
 *  matching across DOM text-node boundaries (so a selection that spans
 *  inline elements like <code> still gets highlighted). Returns a cleanup
 *  function that removes the spans and restores text nodes.
 *
 *  Algorithm, per tag (the snapshot is re-walked for EVERY tag — a
 *  previous tag's splitText leaves the old offsets pointing at truncated
 *  nodes, so reusing one snapshot across tags throws IndexSizeError when
 *  two tags land in the same text node):
 *  1. Collect every text node under `container` via TreeWalker.
 *  2. Concatenate their content into one flat string, recording each
 *     node's [start,end) offset in the flat string.
 *  3. indexOf(selectedText) in the flat string and map the
 *     [hit, hit+len) range back to a sequence of text nodes.
 *  4. For each text node fully or partially inside the range, splitText
 *     at the boundaries and wrap the targeted portion in a
 *     `<span class="inline-tag-highlight">`. Every wrapping span carries
 *     `data-tag-id` so the gutter can compute the union bbox of the
 *     whole selection (a multi-line selection wraps several spans). */
export function applyTagHighlights(
  container: HTMLElement,
  tags: InlineTag[],
): () => void {
  if (tags.length === 0) return () => {};

  const createdSpans: HTMLSpanElement[] = [];

  for (const tag of tags) {
    if (!tag.selectedText) continue;

    const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
    const textNodes: Text[] = [];
    let n: Node | null;
    while ((n = walker.nextNode())) textNodes.push(n as Text);

    const offsets: { node: Text; start: number; end: number }[] = [];
    let cursor = 0;
    for (const node of textNodes) {
      const len = node.textContent?.length ?? 0;
      offsets.push({ node, start: cursor, end: cursor + len });
      cursor += len;
    }
    const fullText = textNodes.map((t) => t.textContent ?? "").join("");

    const hit = fullText.indexOf(tag.selectedText);
    if (hit < 0) continue;
    const hitEnd = hit + tag.selectedText.length;

    let lastSpan: HTMLSpanElement | null = null;
    for (const info of offsets) {
      if (info.end <= hit) continue;
      if (info.start >= hitEnd) break;

      const localStart = Math.max(0, hit - info.start);
      const localEnd = Math.min(info.end - info.start, hitEnd - info.start);
      if (localEnd <= localStart) continue;

      const rest =
        localStart > 0 ? info.node.splitText(localStart) : info.node;
      const wrapLen = localEnd - localStart;
      if (wrapLen < (rest.textContent?.length ?? 0)) {
        rest.splitText(wrapLen);
      }
      const span = document.createElement("span");
      span.className = "inline-tag-highlight";
      if (tag.id === focusedTagId) span.classList.add(FOCUSED_CLASS);
      if (tag.id === PENDING_TAG_ID) span.classList.add(SELECTED_CLASS);
      span.dataset.tagId = tag.id;
      rest.parentNode?.insertBefore(span, rest);
      span.appendChild(rest);
      createdSpans.push(span);
      lastSpan = span;
    }
    // Footnote-style reference marker, rendered by CSS ::after from this
    // attribute. Must NOT be a DOM text node: the overlay/highlight
    // matchers and clipboard copy flatten text nodes, and a literal "1"
    // would pollute both. An in-flow ::after lands at the end of the
    // commented text — right in LTR, left in RTL — for free.
    if (lastSpan && tag.displayNumber !== undefined) {
      lastSpan.dataset.refNumber = String(tag.displayNumber);
    }
  }

  return () => {
    for (const span of createdSpans) {
      const parent = span.parentNode;
      if (!parent) continue;
      while (span.firstChild) {
        parent.insertBefore(span.firstChild, span);
      }
      parent.removeChild(span);
      parent.normalize();
    }
  };
}
