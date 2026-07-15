import type { SendMode } from "../types";
import type { InlineTag } from "../types/inlineTag";
import { mergeTagsIntoPrompt } from "./inlineTagsPrompt";
import {
  buildOpenFilesPreamble,
  buildOpenFilesStateKey,
  type OpenFileSnapshot,
} from "./openFilesPreamble";
export interface BuildFinalPromptInput {
  prompt: string;
  tags: InlineTag[];
  sendMode: SendMode;
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
  openFileSnapshots = [],
  previousOpenFilesStateKey = null,
}: BuildFinalPromptInput): FinalPromptResult {
  const openFilesStateKey = buildOpenFilesStateKey(openFileSnapshots);

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
