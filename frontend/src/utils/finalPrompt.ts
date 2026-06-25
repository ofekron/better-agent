import type { SendMode } from "../types";
import type { InlineTag } from "../types/inlineTag";
import { mergeTagsIntoPrompt } from "./inlineTagsPrompt";
import {
  buildOpenFilesPreamble,
  buildOpenFilesStateKey,
  type OpenFileSnapshot,
} from "./openFilesPreamble";
import { applyQueuedInlineTags } from "./queuedPreview";

export interface FinalPromptQueuedItem {
  preview: string;
}

export interface BuildFinalPromptInput {
  prompt: string;
  tags: InlineTag[];
  sendMode: SendMode;
  latestQueued?: FinalPromptQueuedItem | null;
  openFileSnapshots?: OpenFileSnapshot[];
  previousOpenFilesStateKey?: string | null;
}

export interface FinalPromptResult {
  prompt: string;
  sendMode: SendMode;
  openFilesStateKey: string;
}

export function buildFinalPrompt({
  prompt,
  tags,
  sendMode,
  latestQueued,
  openFileSnapshots = [],
  previousOpenFilesStateKey = null,
}: BuildFinalPromptInput): FinalPromptResult {
  const openFilesStateKey = buildOpenFilesStateKey(openFileSnapshots);

  if (sendMode === "queue" && tags.length > 0 && latestQueued) {
    const queuedWithTags = applyQueuedInlineTags(latestQueued.preview, tags);
    return {
      prompt: prompt.trim()
        ? `${queuedWithTags}\n\n${prompt.trim()}`
        : queuedWithTags,
      sendMode: "alter",
      openFilesStateKey,
    };
  }

  const withTags = mergeTagsIntoPrompt(prompt, tags);
  const openFilesPreamble =
    openFilesStateKey && openFilesStateKey !== previousOpenFilesStateKey
      ? buildOpenFilesPreamble(openFileSnapshots)
      : "";
  return {
    prompt: openFilesPreamble ? `${openFilesPreamble}\n${withTags}` : withTags,
    sendMode,
    openFilesStateKey,
  };
}
