/** File-editing overlay state.
 *
 * Set when the user opens a file in editing mode. While non-null,
 * App.tsx swaps the main panel for the FileEditorOverlay (multi-file
 * live-diff on the left, chat against the editor session on the right).
 * One file-editing session per project cwd — the set grows as the user
 * opens more files. Cleared on Done / Cancel.
 */
export interface FileEditingState {
  sessionId: string;
  /** The files currently in the edit set (backend-owned). */
  filePaths: string[];
  /** Per-file diff baseline (path → original content at add time). */
  originalContents: Record<string, string>;
  fileDiscussions: FileDiscussion[];
}
import type { FileDiscussion } from "../types";
