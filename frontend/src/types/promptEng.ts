/** Prompt-engineering overlay state.
 *
 * Set when the user clicks "⚙ Engineer my prompt" in the input area.
 * While non-null, App.tsx swaps the main panel + right panel for the
 * prompt-engineer extension overlay (chat against the eng session on
 * the left, live-diff prompt.md on the right). Cleared on Cancel or Send.
 */
export interface PromptEngState {
  engSessionId: string;
  parentSessionId: string;
  tempFilePath: string;
  /** The user's original draft — diff baseline for the FileViewer. */
  originalContent: string;
  mode: "fork" | "new";
}
