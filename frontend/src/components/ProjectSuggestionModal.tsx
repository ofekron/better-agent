import { useBackButtonDismiss } from "../hooks/useBackButtonDismiss";
import { scaledFontSize } from "../utils/typography";

export interface ProjectSuggestion {
  target_cwd: string;
  score: number;
  margin: number;
}

interface Props {
  suggestion: ProjectSuggestion;
  currentName: string;
  targetName: string;
  onMove: () => void;
  onSendHere: () => void;
  onCancel: () => void;
}

export function ProjectSuggestionModal({
  suggestion,
  currentName,
  targetName,
  onMove,
  onSendHere,
  onCancel,
}: Props) {
  useBackButtonDismiss(true, onCancel);
  const confidence = Math.round(suggestion.score * 100);
  const bodyTextStyle = {
    lineHeight: "1.5",
    color: "var(--text-secondary)",
    overflowWrap: "anywhere",
  } as const;

  return (
    <div className="modal-overlay" onClick={onCancel}>
      <div
        className="modal-content"
        style={{ maxWidth: "440px" }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-header">
          <h2>Check project</h2>
          <button className="modal-close" onClick={onCancel}>
            &times;
          </button>
        </div>
        <div className="modal-body">
          <p style={{ ...bodyTextStyle, margin: "16px 0 8px" }}>
            You sent this to:{" "}
            <strong style={{ color: "var(--text-primary)" }}>{currentName}</strong>
            <br />
            I think you meant to send it to:{" "}
            <strong style={{ color: "var(--text-primary)" }}>{targetName}</strong>{" "}
            ({confidence}% match).
          </p>
          <p style={{ ...bodyTextStyle, margin: "0 0 8px" }}>
            Move this session there before starting?
          </p>
          <p style={{ margin: "0 0 8px", fontSize: scaledFontSize(12), color: "var(--text-tertiary)", overflowWrap: "anywhere" }}>
            {suggestion.target_cwd}
          </p>
        </div>
        <div className="modal-footer">
          <button type="button" className="btn-secondary" onClick={onSendHere}>
            Send here
          </button>
          <button type="button" className="btn-primary" onClick={onMove} autoFocus>
            Move &amp; send
          </button>
        </div>
      </div>
    </div>
  );
}
