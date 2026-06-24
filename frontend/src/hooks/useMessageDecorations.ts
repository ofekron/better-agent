import { useLayoutEffect, type RefObject } from "react";
import type { AdvSyncOverlay } from "../types";
import type { InlineTag } from "../types/inlineTag";
import { applyAdvSyncOverlays } from "../utils/advSyncOverlays";
import { applyTagHighlights } from "../utils/tagHighlights";

interface MessageDecorationParams {
  tags?: InlineTag[];
  advSyncOverlays?: AdvSyncOverlay[];
  onAdvSyncClick?: (overlay: AdvSyncOverlay) => void;
  /** Token that changes whenever the decorated DOM subtree is rebuilt.
   *  It is the remount `key` on the message-content div: a stub message
   *  that lazily fetches its full form, a reconcile re-fetch, or a
   *  streaming frame all swap `effectiveMessage` for a new object,
   *  bumping this token and remounting the body. The injected highlight
   *  and overlay spans are imperative — React does not own them — so
   *  every remount discards them. This hook re-applies them whenever
   *  the token changes, which is why it is a dependency. */
  revision: string | number;
}

/** Apply inline-tag highlights and adv-sync overlays to a rendered
 *  message body. Both are imperative post-render DOM mutations, so this
 *  hook owns their full lifecycle: it re-runs — clearing the previous
 *  pass then re-applying — whenever the inputs change OR the body is
 *  remounted (`revision` changes). Runs the tag pass before the overlay
 *  pass so the overlay's TreeWalker can skip nodes inside
 *  `.inline-tag-highlight`; cleanup runs them in reverse so the tag
 *  cleanup's `normalize()` never sees overlay spans between siblings. */
export function useMessageDecorations(
  containerRef: RefObject<HTMLElement | null>,
  { tags, advSyncOverlays, onAdvSyncClick, revision }: MessageDecorationParams,
): void {
  useLayoutEffect(() => {
    const hasTags = !!(tags && tags.length > 0);
    const hasOverlays = !!(advSyncOverlays && advSyncOverlays.length > 0);
    if (!containerRef.current || (!hasTags && !hasOverlays)) return;
    let tagCleanup: (() => void) | undefined;
    let overlayCleanup: (() => void) | undefined;
    if (hasTags) {
      tagCleanup = applyTagHighlights(containerRef.current, tags!);
    }
    if (hasOverlays && onAdvSyncClick) {
      overlayCleanup = applyAdvSyncOverlays(
        containerRef.current,
        advSyncOverlays!,
        onAdvSyncClick,
      );
    }
    return () => {
      overlayCleanup?.();
      tagCleanup?.();
    };
  }, [tags, advSyncOverlays, onAdvSyncClick, revision, containerRef]);
}
