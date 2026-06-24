import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { FileEditor, type FileAnchorComment } from "./FileEditor";
import type { ChatMessage, FileDiscussion } from "../types";

interface Props {
  /** The edit set (backend-owned). One tab per file. */
  filePaths: string[];
  /** Per-file diff baseline (path → original content at add time). */
  originalContents: Record<string, string>;
  /** Aggregate count of file-anchored comments queued on the session,
   * surfaced as a hint chip in each file's comment bar. */
  pendingTagCount?: number;
  /** Queue a file-anchored comment. The anchor already carries the
   * file path, so a single handler serves every tab. */
  onSubmitComment: (anchor: FileAnchorComment) => Promise<void>;
  fileDiscussions?: FileDiscussion[];
  sessionMessages?: ChatMessage[];
  onStartDiscussion?: (filePath: string, line: number) => Promise<FileDiscussion>;
  onPatchDiscussion?: (discussionId: string, patch: Partial<FileDiscussion>) => Promise<void>;
  onSendDiscussionMessage?: (
    discussionId: string,
    prompt: string,
    clientId: string,
  ) => Promise<void>;
}

function baseName(p: string): string {
  return p.replace(/\/+$/, "").split("/").pop() || p;
}

/** Multi-file diff/edit surface for a file-editing session. Renders a
 * tab strip and ONE FileEditor per file, all kept mounted (inactive
 * tabs hidden, not unmounted) with a fixed `tempFilePath` each. Keeping
 * every instance mounted is load-bearing: FileEditor flushes a save
 * to disk on `tempFilePath` change, so swapping the path on one shared
 * instance would write one file's (poll-fed) content over another. */
export function MultiFileEditor({
  filePaths,
  originalContents,
  pendingTagCount = 0,
  onSubmitComment,
  fileDiscussions = [],
  sessionMessages = [],
  onStartDiscussion,
  onPatchDiscussion,
  onSendDiscussionMessage,
}: Props) {
  const { t } = useTranslation();
  const [activePath, setActivePath] = useState<string>(filePaths[0] ?? "");
  const prevLenRef = useRef(filePaths.length);

  // The set is backend-owned and grows via WS. When a file was just
  // added, focus it (that's what the user opened / the agent was told
  // about). Otherwise just keep the active tab valid.
  useEffect(() => {
    const grew = filePaths.length > prevLenRef.current;
    prevLenRef.current = filePaths.length;
    if (grew) {
      setActivePath(filePaths[filePaths.length - 1]);
      return;
    }
    if (!filePaths.includes(activePath)) {
      setActivePath(filePaths[0] ?? "");
    }
  }, [filePaths, activePath]);

  if (filePaths.length === 0) return null;

  return (
    <div className="multi-file-editor">
      <div
        className="multi-file-tabs"
        role="tablist"
        aria-label={t("multiFile.tabsLabel")}
      >
        {filePaths.map((p) => (
          <button
            key={p}
            type="button"
            role="tab"
            aria-selected={p === activePath}
            className={`multi-file-tab ${p === activePath ? "active" : ""}`}
            onClick={() => setActivePath(p)}
            title={p}
            data-testid={`multi-file-tab-${baseName(p)}`}
          >
            {baseName(p)}
          </button>
        ))}
      </div>
      <div className="multi-file-body">
        {filePaths.map((p) => (
          <div
            key={p}
            className="multi-file-pane"
            style={{ display: p === activePath ? "flex" : "none" }}
            data-testid={`multi-file-pane-${baseName(p)}`}
          >
            <FileEditor
              tempFilePath={p}
              originalContent={originalContents[p] ?? ""}
              pendingTagCount={pendingTagCount}
              onSubmitComment={onSubmitComment}
              diskWritable={false}
              fileDiscussions={fileDiscussions.filter((d) => d.file_path === p)}
              sessionMessages={sessionMessages}
              onStartDiscussion={onStartDiscussion}
              onPatchDiscussion={onPatchDiscussion}
              onSendDiscussionMessage={onSendDiscussionMessage}
            />
          </div>
        ))}
      </div>
    </div>
  );
}
