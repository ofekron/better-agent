import type { InlineTag } from "../types/inlineTag";

export function scrollCommentTargetIntoView(
  id: string,
  tags: InlineTag[],
  documentRef: Document = document,
): void {
  const scrollEl = documentRef.querySelector(".chat-messages") as HTMLElement | null;
  if (!scrollEl) return;
  const targetTop = commentTargetTop(id, tags, scrollEl, documentRef);
  if (targetTop === null) return;
  const scrollRect = scrollEl.getBoundingClientRect();
  scrollEl.scrollTo({
    top: scrollEl.scrollTop + targetTop - scrollRect.top - 24,
    behavior: "smooth",
  });
}

function commentTargetTop(
  id: string,
  tags: InlineTag[],
  scrollEl: HTMLElement,
  documentRef: Document,
): number | null {
  const spans = documentRef.querySelectorAll<HTMLElement>(
    `.inline-tag-highlight[data-tag-id="${CSS.escape(id)}"]`,
  );
  if (spans.length > 0) {
    let minTop = Infinity;
    for (const span of spans) {
      const rect = span.getBoundingClientRect();
      if (rect.top < minTop) minTop = rect.top;
    }
    return minTop;
  }

  const tag = tags.find((item) => item.id === id);
  if (!tag?.messageId) return null;
  const messageEl = scrollEl.querySelector(
    `[data-message-id="${CSS.escape(tag.messageId)}"]`,
  ) as HTMLElement | null;
  return messageEl?.getBoundingClientRect().top ?? null;
}
