import type { FileAttachment, PastedImage, QueuedPrompt } from "src/types";
import { filePayloadToAttachment, imagePayloadToPastedImage } from "src/utils/imageAttach";

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

// Single source of truth for "this queued kind lands in the banner". The
// optimistic "Sending…" bubble is cleared for banner kinds (the banner
// replaces it), so callers route through here instead of re-listing kinds
// and drifting — the original bug only checked "queued_behind".
export function isBannerQueuedKind(kind: string | undefined): boolean {
  return (
    kind !== undefined &&
    VISIBLE_QUEUE_KINDS.has(kind as QueuedPrompt["kind"])
  );
}

export function queuedPromptToVisibleBanner(
  prompt: QueuedPrompt,
): QueuedBannerState | null {
  if (!isBannerQueuedKind(prompt.kind)) return null;
  return {
    id: prompt.id,
    ...(prompt.client_id !== undefined ? { clientId: prompt.client_id } : {}),
    preview: prompt.content,
    ...(prompt.images?.length
      ? { images: prompt.images.map(imagePayloadToPastedImage) }
      : {}),
    ...(prompt.files?.length
      ? { files: prompt.files.map(filePayloadToAttachment) }
      : {}),
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
