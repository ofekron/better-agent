import type { RefObject } from "react";
import { Editor } from "@monaco-editor/react";
import type { editor } from "monaco-editor";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import { markdownLinkifyComponents } from "../utils/linkifyFilePaths";

export function MarkdownFileEditor({
  value,
  editing,
  readOnly,
  fontSize,
  theme,
  editClassName,
  formattedClassName,
  editTestId,
  formattedTestId,
  renderedRef,
  autoFocus,
  onRequestEdit,
  onMount,
  onChange,
}: {
  value: string;
  editing: boolean;
  readOnly: boolean;
  fontSize: number;
  theme: string;
  editClassName: string;
  formattedClassName: string;
  editTestId: string;
  formattedTestId: string;
  renderedRef?: RefObject<HTMLDivElement | null>;
  autoFocus?: boolean;
  onRequestEdit?: () => void;
  onMount: (editor: editor.IStandaloneCodeEditor) => void;
  onChange?: (value: string) => void;
}) {
  if (editing) {
    return (
      <div className={editClassName} data-testid={editTestId}>
        <Editor
          height="100%"
          language="markdown"
          value={value}
          theme={theme}
          onMount={(ed) => {
            onMount(ed);
            if (autoFocus) ed.focus();
          }}
          onChange={(next) => {
            if (!readOnly) onChange?.(next ?? "");
          }}
          options={{
            readOnly,
            minimap: { enabled: false },
            fontSize,
            lineNumbers: "on",
            scrollBeyondLastLine: false,
            wordWrap: "on",
            automaticLayout: true,
          }}
        />
      </div>
    );
  }

  return (
    <div
      className={formattedClassName}
      ref={renderedRef}
      onDoubleClick={onRequestEdit}
      data-testid={formattedTestId}
    >
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeHighlight]}
        components={markdownLinkifyComponents()}
      >
        {value}
      </ReactMarkdown>
    </div>
  );
}
