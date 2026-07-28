import { useLayoutEffect, type RefObject } from "react";
import type { InlineTag } from "../types/inlineTag";
import { applyTagHighlights } from "../utils/tagHighlights";

interface MessageDecorationParams {
  tags?: InlineTag[];
  /** Token that changes whenever the decorated DOM subtree is rebuilt.
   *  It is the remount `key` on the message-content div: a stub message
   *  that lazily fetches its full form, a reconcile re-fetch, or a
   *  streaming frame all swap `effectiveMessage` for a new object,
   *  bumping this token and remounting the body. The injected highlight
   *  spans are imperative — React does not own them — so
   *  every remount discards them. This hook re-applies them whenever
   *  the token changes, which is why it is a dependency. */
  revision: string | number;
}

/** Apply inline-tag highlights to a rendered message body. The spans are
 * imperative post-render DOM mutations, so this hook owns their full
 * lifecycle and reapplies them whenever inputs or the body revision change. */
export function useMessageDecorations(
  containerRef: RefObject<HTMLElement | null>,
  { tags, revision }: MessageDecorationParams,
): void {
  useLayoutEffect(() => {
    const hasTags = !!(tags && tags.length > 0);
    if (!containerRef.current || !hasTags) return;
    return applyTagHighlights(containerRef.current, tags!);
  }, [tags, revision, containerRef]);
}
