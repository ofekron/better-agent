import type { FileAttachment, PastedImage, QueuedPrompt } from "src/types";

export type QueuedBannerState = {
  id: string;
  clientId?: string | null;
  preview: string;
  images?: PastedImage[];
  imagesCount?: number;
  files?: FileAttachment[];
  filesCount?: number;
};

const VISIBLE_QUEUE_KINDS = new Set<QueuedPrompt["kind"]>([
  "queued_behind",
  "interrupt",
]);

export function queuedPromptToVisibleBanner(
  prompt: QueuedPrompt,
): QueuedBannerState | null {
  if (!VISIBLE_QUEUE_KINDS.has(prompt.kind)) return null;
  return {
    id: prompt.id,
    ...(prompt.client_id !== undefined ? { clientId: prompt.client_id } : {}),
    preview: prompt.content,
    imagesCount: prompt.images_count,
    filesCount: prompt.files_count,
  };
}

export function visibleQueuedPromptBanners(
  prompts: readonly QueuedPrompt[] | undefined,
): QueuedBannerState[] {
  return (prompts ?? []).flatMap((prompt) => {
    const banner = queuedPromptToVisibleBanner(prompt);
    return banner ? [banner] : [];
  });
}
