import type { FileAttachment, PastedImage, SendMode } from "../types";

const QUEUE_MERGE_SEPARATOR = "\n\n---\n\n";

export interface SendPromptFormInput {
  finalPrompt: string;
  sendMode: SendMode;
  existingQueuedPreview?: string | null;
  existingPendingText?: string | null;
}

export interface SendPromptForm {
  prompt: string;
  replacedQueuedPrompt: boolean;
}

export function buildSendPromptForm({
  finalPrompt,
  sendMode,
  existingQueuedPreview,
  existingPendingText,
}: SendPromptFormInput): SendPromptForm {
  if (sendMode !== "queue") {
    return { prompt: finalPrompt, replacedQueuedPrompt: false };
  }

  const existingText = existingQueuedPreview ?? existingPendingText;
  if (!existingText) {
    return { prompt: finalPrompt, replacedQueuedPrompt: false };
  }

  return {
    prompt: `${existingText}${QUEUE_MERGE_SEPARATOR}${finalPrompt}`,
    replacedQueuedPrompt: true,
  };
}

/**
 * Merge the previously-queued prompt's attachments with the current send's.
 * A queue merge cancels the old backend entry and re-dispatches a single
 * prompt; without folding the prior attachments forward they'd be lost.
 * The previous attachments precede the current ones, mirroring the text
 * merge order. Returns the original current arrays untouched when there are
 * no prior attachments.
 */
export function mergeQueuedAttachments<
  I extends PastedImage,
  F extends FileAttachment,
>(
  prevImages: I[] | undefined | null,
  prevFiles: F[] | undefined | null,
  currentImages: I[],
  currentFiles: F[],
): { images: I[]; files: F[] } {
  const prevI = prevImages ?? [];
  const prevF = prevFiles ?? [];
  if (prevI.length === 0 && prevF.length === 0) {
    return { images: currentImages, files: currentFiles };
  }
  return {
    images: [...prevI, ...currentImages],
    files: [...prevF, ...currentFiles],
  };
}
