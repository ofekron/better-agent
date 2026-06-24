import { useState } from "react";
import { useTranslation } from "react-i18next";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { FileFocus } from "../types";
import { markdownLinkifyComponents } from "../utils/linkifyFilePaths";

interface Props {
  thought: string;
  onFileClick?: (path: string, focus?: FileFocus) => void;
}

export function ThinkingBlock({ thought, onFileClick }: Props) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);

  const preview =
    thought.length > 120 ? thought.slice(0, 120) + "..." : thought;

  return (
    <div className="thinking-block" onClick={() => setExpanded(!expanded)}>
      <span className="thinking-indicator">
        {expanded ? "v" : ">"} {t("thinking.thinking")}
      </span>
      <div className="thinking-content">
        <ReactMarkdown
          remarkPlugins={[remarkGfm]}
          components={markdownLinkifyComponents(onFileClick)}
        >
          {expanded ? thought : preview}
        </ReactMarkdown>
      </div>
    </div>
  );
}
