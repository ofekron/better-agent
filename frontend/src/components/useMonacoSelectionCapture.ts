import { useEffect } from "react";
import type { editor } from "monaco-editor";

export interface MonacoSelectionRange {
  startLine: number;
  endLine: number;
  startCol: number;
  endCol: number;
}

export function useMonacoSelectionCapture({
  editor,
  enabled,
  onCapture,
}: {
  editor: editor.IStandaloneCodeEditor | null;
  enabled: boolean;
  onCapture: (selection: MonacoSelectionRange) => void;
}) {
  useEffect(() => {
    if (!enabled || !editor) return;

    const capture = () => {
      const sel = editor.getSelection();
      if (!sel) return;
      if (
        sel.startLineNumber === sel.endLineNumber &&
        sel.startColumn === sel.endColumn
      ) {
        return;
      }
      onCapture({
        startLine: sel.startLineNumber,
        endLine: sel.endLineNumber,
        startCol: sel.startColumn,
        endCol: sel.endColumn,
      });
    };

    const mouseUp = editor.onMouseUp(capture);
    const keyUp = editor.onKeyUp((event: { shiftKey?: boolean }) => {
      if (event.shiftKey) capture();
    });
    return () => {
      mouseUp.dispose();
      keyUp.dispose();
    };
  }, [editor, enabled, onCapture]);
}
