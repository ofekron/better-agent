import { useMemo, useState, useCallback } from "react";
import { useTranslation } from "react-i18next";
import type { FileEditingState } from "../types/fileEditing";
import { WorkingModeLayout } from "./WorkingModeLayout";
import { useBackButtonDismiss } from "../hooks/useBackButtonDismiss";

/** Width of the trimmed sidebar while the file-edit overlay is active.
 * Mirrors the override in App.tsx so the 50/50 split below sees the
 * actually-rendered remainder. */
const FILE_EDIT_SIDEBAR_WIDTH = 200;

interface Props {
  state: FileEditingState;
  /** True for top-level user-facing file-mode sessions (new-session
   * modal entry); the Done button is hidden in this flavor — the
   * user leaves by selecting another session in the sidebar. False
   * for the temporal flavor (project-tree AI Edit). */
  persistent: boolean;
  /** Close the overlay, optionally tearing down the session.
   * Not invoked when `persistent` is true (the button isn't rendered). */
  onDone: () => Promise<void>;
  onCancel: () => Promise<void>;
  /** Slots — App.tsx renders the live-diff editor first and the
   * editor-session-bound chat beside it. */
  chatSlot: React.ReactNode;
  fileViewerSlot: React.ReactNode;
}

export function FileEditorOverlay({
  state,
  persistent,
  onDone,
  onCancel,
  chatSlot,
  fileViewerSlot,
}: Props) {
  const { t } = useTranslation();
  const [busy, setBusy] = useState<"done" | "cancel" | null>(null);
  // Hardware back exits the editor overlay. onCancel is async (tears
  // down the backend session) and the overlay stays mounted until it
  // resolves. Push an absorber sentinel so a second back-press during
  // the in-flight window doesn't bubble past the overlay (which would
  // exit the app on a fresh launch).
  const handleBack = useCallback(async () => {
    window.history.pushState({ __cancelInFlight: true }, "");
    try {
      await onCancel();
    } finally {
      const state = window.history.state as { __cancelInFlight?: boolean } | null;
      if (state?.__cancelInFlight) {
        window.history.replaceState(null, "");
      }
    }
  }, [onCancel]);
  useBackButtonDismiss(true, handleBack);

  // 50/50 split of the (trimmed-sidebar-adjusted) viewport width on
  // first open. Computed ONCE per mount via useMemo so that subsequent
  // window resizes don't keep nudging the divider mid-session; useResizable's
  // own localStorage persistence takes over after the user drags.
  const initialFileViewerWidth = useMemo(
    () =>
      Math.max(
        500,
        Math.floor((window.innerWidth - FILE_EDIT_SIDEBAR_WIDTH) / 2),
      ),
    [],
  );

  const handleDone = async () => {
    if (busy) return;
    setBusy("done");
    try {
      await onDone();
    } finally {
      setBusy(null);
    }
  };

  const handleCancel = async () => {
    if (busy) return;
    setBusy("cancel");
    try {
      await onCancel();
    } finally {
      setBusy(null);
    }
  };

  const first = state.filePaths[0] ?? "";
  const fileName = first.replace(/\/+$/, "").split("/").pop() || first;
  const badge =
    state.filePaths.length > 1
      ? t("fileEditor.badgeMulti", { count: state.filePaths.length })
      : t("fileEditor.badge", { fileName });

  return (
    <WorkingModeLayout
      storagePrefix="fileEditor"
      defaultSize={initialFileViewerWidth}
      badge={<>{badge}</>}
      actions={
        <>
          <button
            type="button"
            className="btn-secondary"
            data-testid="file-editor-cancel-btn"
            onClick={handleCancel}
            disabled={busy !== null}
          >
            {busy === "cancel" ? t("fileEditor.discarding") : t("fileEditor.discard")}
          </button>
          {!persistent && (
            <button
              type="button"
              className="btn-primary"
              data-testid="file-editor-done-btn"
              onClick={handleDone}
              disabled={busy !== null}
              style={{ marginInlineStart: 8 }}
            >
              {busy === "done" ? t("fileEditor.closing") : t("fileEditor.done")}
            </button>
          )}
        </>
      }
      chatSlot={chatSlot}
      fileViewerSlot={fileViewerSlot}
      fileFirst
      testId="file-editor-overlay"
    />
  );
}
