import { useState, useCallback } from "react";
import Icon from "./Icon";
import { useTranslation } from "react-i18next";
import { API } from "../api";

const VIDEO_EXTS = new Set(["mp4", "webm", "mov", "avi", "mkv", "m4v", "ogv", "3gp"]);
const PDF_EXTS = new Set(["pdf"]);

export function getMediaType(path: string): "video" | "pdf" | null {
  const dot = path.lastIndexOf(".");
  if (dot === -1 || dot === path.length - 1) return null;
  const ext = path.slice(dot + 1).toLowerCase();
  if (PDF_EXTS.has(ext)) return "pdf";
  if (VIDEO_EXTS.has(ext)) return "video";
  return null;
}

function rawUrl(path: string, nodeId: string = "primary"): string {
  return `${API}/api/file/raw?path=${encodeURIComponent(path)}&node_id=${encodeURIComponent(nodeId)}`;
}

interface Props {
  path: string;
  mediaType: "video" | "pdf";
  onFileClick?: (path: string) => void;
}

export function MediaPreviewInline({ path, mediaType, onFileClick }: Props) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);
  const fileName = path.split("/").pop() ?? path;

  const handleToggle = useCallback(
    (e: React.MouseEvent) => {
      e.stopPropagation();
      setExpanded((v) => !v);
    },
    [],
  );

  const handleNameClick = useCallback(
    (e: React.MouseEvent) => {
      e.stopPropagation();
      onFileClick?.(path);
    },
    [onFileClick, path],
  );

  return (
    <div className={`media-preview-inline${expanded ? " expanded" : ""}`}>
      <div className="media-preview-card">
        <span
          className={`media-preview-icon icon-${mediaType}`}
          role="img"
          aria-label={mediaType}
        >
          {mediaType === "pdf" ? <Icon name="memo" size={30} /> : <Icon name="film" size={30} />}
        </span>
        <span
          className="media-preview-name"
          role="link"
          tabIndex={0}
          onClick={handleNameClick}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              handleNameClick(e as unknown as React.MouseEvent);
            }
          }}
        >
          {fileName}
        </span>
        <button className="media-preview-expand" onClick={handleToggle}>
          {expanded ? t("mediaPreview.collapse") : t("mediaPreview.preview")}
        </button>
      </div>
      {expanded && (
        <div className="media-preview-content">
          {mediaType === "pdf" ? (
            <iframe
              src={rawUrl(path)}
              title={fileName}
              className="media-preview-pdf-iframe"
            />
          ) : (
            <video
              src={rawUrl(path)}
              controls
              preload="metadata"
              className="media-preview-video-player"
            >
              {t("mediaPreview.videoNotSupported")}
            </video>
          )}
        </div>
      )}
    </div>
  );
}
