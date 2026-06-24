import { useRef } from "react";
import { useTranslation } from "react-i18next";
import { ProgressButton } from "../progress/ProgressButton";
import { trackPromise, useOpProgress } from "../progress/store";
import { usePersistedDraft } from "../hooks/usePersistedDraft";

/** Selection captured from a Monaco editor — has line:col positions. */
export interface MonacoSelection {
  kind: "monaco";
  startLine: number;
  endLine: number;
  startCol: number;
  endCol: number;
}

/** Selection captured from a non-Monaco rendered view (markdown HTML,
 * CSV/TSV table) — only the copied text is positionally meaningful. */
export interface TextSelection {
  kind: "text";
  selectedText: string;
}

export type CommentSelection = MonacoSelection | TextSelection;

export interface SubmittedComment {
  selection: CommentSelection;
  comment: string;
}

interface Props {
  /** Active selection the user just made; null when nothing pending. */
  selection: CommentSelection | null;
  /** Called when the user clicks "Queue comment" with non-empty text.
   * Should resolve when the tag is persisted; the bar resets after. */
  onSubmit: (c: SubmittedComment) => Promise<void>;
  /** Drop the pending selection without submitting. */
  onCancel: () => void;
  /** Optional count of file-anchored comments already queued for this
   * file (or session, depending on caller). Surfaced as a chip in the
   * empty-state hint so the user knows their previous comment landed. */
  pendingTagCount?: number;
  /** Stable identifier (e.g. file path) under which the in-progress comment
   * draft is auto-saved to localStorage, so it survives unmount / file
   * switches and is restored when the user returns. */
  draftKey?: string | null;
}

function describeSelection(sel: CommentSelection): string {
  if (sel.kind === "monaco") {
    if (sel.startLine === sel.endLine) {
      return `line ${sel.startLine} (col ${sel.startCol}–${sel.endCol})`;
    }
    return `${sel.startLine}:${sel.startCol} → ${sel.endLine}:${sel.endCol}`;
  }
  const t = sel.selectedText.replace(/\s+/g, " ").trim();
  return t.length > 60 ? `“${t.slice(0, 60)}…”` : `“${t}”`;
}

export function FileCommentBar({
  selection,
  onSubmit,
  onCancel,
  pendingTagCount = 0,
  draftKey = null,
}: Props) {
  const { t } = useTranslation();
  const [comment, setComment, clearComment] = usePersistedDraft(
    draftKey ? `comment-draft:${draftKey}` : null,
  );
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const opId = "comment:submit";
  const { inflight: submitting } = useOpProgress(opId);

  const handleSubmit = async () => {
    if (!selection || !comment.trim() || submitting) return;
    try {
      await trackPromise(opId, () =>
        onSubmit({ selection, comment: comment.trim() }),
      ).promise;
      clearComment();
      // Return focus to the main prompt bar so the user can press Enter
      // to send immediately without clicking.
      (
        document.querySelector<HTMLTextAreaElement>('[data-testid="input-textarea"]')
        ?? document.querySelector<HTMLTextAreaElement>("textarea.input-prompt")
      )?.focus();
    } catch (e) {
      // eslint-disable-next-line no-alert
      alert(`${t("fileComment.failedToAdd")}${e instanceof Error ? e.message : String(e)}`);
    }
  };

  return (
    <div className="file-comment-bar">
      {selection ? (
        <>
          <div className="file-comment-bar-anchor">
            {t("fileComment.commentOn", { selection: describeSelection(selection) })}
          </div>
          <textarea
            ref={inputRef}
            className="file-comment-input"
            placeholder={t("fileComment.placeholder")}
            value={comment}
            onChange={(e) => setComment(e.target.value)}
            onKeyDown={(e) => {
              if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
                e.preventDefault();
                void handleSubmit();
              }
            }}
            disabled={submitting}
            rows={2}
          />
          <ProgressButton
            opId={opId}
            type="button"
            className="btn-primary"
            onClick={handleSubmit}
            extraDisabled={!comment.trim()}
            loadingChildren={t("fileComment.queueing")}
          >
            {t("fileComment.queueComment")}
          </ProgressButton>
          <ProgressButton
            opId={opId}
            type="button"
            className="btn-secondary"
            onClick={() => {
              clearComment();
              onCancel();
            }}
          >
            Cancel
          </ProgressButton>
        </>
      ) : (
        <span className="file-comment-bar-hint">
          {pendingTagCount > 0 ? (
            t("fileComment.queuedHint", { count: pendingTagCount })
          ) : (
            t("fileComment.emptyHint")
          )}
        </span>
      )}
    </div>
  );
}
