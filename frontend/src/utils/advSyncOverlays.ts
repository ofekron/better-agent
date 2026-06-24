import type { AdvSyncOverlay } from "../types";

/** Apply adversarial-sync overlay spans on top of a rendered message
 *  body. Mirrors `applyTagHighlights` in shape (TreeWalker → flatten
 *  text-node string → indexOf → splitText + wrap), but the wrapper
 *  span behaviour is status-aware:
 *
 *  - running   : keep the original text in place, append a small
 *                "⇄ N/M" badge to indicate progress, title shows the
 *                original.
 *  - converged : replace the span's text with `agreed_text`; hover
 *                shows the original; click opens the side-by-side
 *                fork view via `onClick(overlay)`.
 *  - failed /
 *    interrupted /
 *    stopped   : muted style with title carrying the reason.
 *
 *  Run AFTER `applyTagHighlights` on the same containerRef. Inline-tag
 *  spans are skipped: any text node that's a descendant of an existing
 *  `.inline-tag-highlight` is excluded from the TreeWalker so overlay
 *  ranges that overlap with tag selections are dropped on the floor
 *  (overlapping ranges with already-wrapped DOM are a known limit;
 *  the user can clear the tag first if they want both).
 *
 *  Returns a cleanup function that removes the spans + badges and
 *  restores the original text content.
 */
export function applyAdvSyncOverlays(
  container: HTMLElement,
  overlays: AdvSyncOverlay[],
  onClick: (overlay: AdvSyncOverlay) => void,
): () => void {
  if (overlays.length === 0) return () => {};

  const createdSpans: HTMLSpanElement[] = [];
  const createdBadges: HTMLSpanElement[] = [];

  // Walk text nodes; skip any that are inside an inline-tag highlight
  // (we don't want to mutate already-wrapped text).
  const walker = document.createTreeWalker(
    container,
    NodeFilter.SHOW_TEXT,
    {
      acceptNode: (node) => {
        let n: Node | null = node.parentNode;
        while (n && n !== container) {
          if (
            n instanceof HTMLElement &&
            n.classList.contains("inline-tag-highlight")
          ) {
            return NodeFilter.FILTER_REJECT;
          }
          n = n.parentNode;
        }
        return NodeFilter.FILTER_ACCEPT;
      },
    },
  );
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

  for (const overlay of overlays) {
    if (!overlay.original_text) continue;
    const hit = fullText.indexOf(overlay.original_text);
    if (hit < 0) continue;
    const hitEnd = hit + overlay.original_text.length;

    // Build the wrapper. The display text is either agreed_text (when
    // converged) or the original (every other state). The span's
    // wrap content is created LAST after all splitText calls below so
    // we can attach a single child instead of preserving the original
    // text nodes — converged state needs textContent fully replaced.
    const wrapSpan = document.createElement("span");
    wrapSpan.className = `adv-sync-span adv-sync-${overlay.status}`;
    wrapSpan.dataset.overlayId = overlay.id;
    if (overlay.status === "converged" && overlay.agreed_text) {
      wrapSpan.textContent = overlay.agreed_text;
      wrapSpan.title =
        overlay.original_text +
        "\n\n(click to open both forks side-by-side)";
      wrapSpan.style.cursor = "pointer";
      const clickHandler = (e: MouseEvent) => {
        e.stopPropagation();
        onClick(overlay);
      };
      wrapSpan.addEventListener("click", clickHandler);
      // Stash the handler so cleanup can remove it.
      (wrapSpan as HTMLElement & { __advSyncCleanup?: () => void })
        .__advSyncCleanup = () =>
          wrapSpan.removeEventListener("click", clickHandler);
    } else if (overlay.status === "running") {
      wrapSpan.title =
        overlay.original_text +
        `\n\nAdversarial sync in progress (round ${overlay.rounds_completed}/${overlay.max_rounds})`;
    } else {
      wrapSpan.title =
        overlay.original_text +
        `\n\nAdversarial sync ${overlay.status}` +
        (overlay.error ? `: ${overlay.error}` : "");
    }

    // For converged: replace the matched range with a single span
    // carrying agreed_text. For non-converged: wrap the original text
    // span sequence so the user still sees their text but with an
    // overlay class + title hover.
    const replaced: Text[] = [];
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
      replaced.push(rest);
    }
    if (replaced.length === 0) continue;
    const first = replaced[0];
    const parent = first.parentNode;
    if (!parent) continue;
    parent.insertBefore(wrapSpan, first);
    if (overlay.status === "converged" && overlay.agreed_text) {
      // wrapSpan already has agreed_text as its child via textContent
      // above; remove the original text nodes from the DOM.
      for (const tn of replaced) tn.remove();
    } else {
      // Move all matched text nodes into the wrapper so the user
      // still sees the original text but inside a styled span.
      for (const tn of replaced) wrapSpan.appendChild(tn);
    }
    createdSpans.push(wrapSpan);

    if (overlay.status === "running") {
      const badge = document.createElement("span");
      badge.className = "adv-sync-badge adv-sync-badge-running";
      badge.textContent = ` ⇄ ${overlay.rounds_completed}/${overlay.max_rounds}`;
      wrapSpan.after(badge);
      createdBadges.push(badge);
    }
  }

  return () => {
    for (const badge of createdBadges) {
      badge.parentNode?.removeChild(badge);
    }
    for (const span of createdSpans) {
      const cleanup = (span as HTMLElement & {
        __advSyncCleanup?: () => void;
      }).__advSyncCleanup;
      if (cleanup) cleanup();
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
