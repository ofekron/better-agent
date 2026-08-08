// Chrome for a `typed_prompt` node's `TurnEntry.provisionalSend` state
// (state.ts) — the native, SurfaceStore-native equivalent of legacy
// MessageBubble.tsx's `MessageStatus` "sending"/"error" block. Reuses its
// exact CSS classes (`.message-status`/`.status-sending`/`.status-error`/
// `.error-block-header`/`.error-block-toggle`/`.error-block-body`/
// `.error-copy-btn`/`.status-retry-btn`, all pre-existing — no new CSS)
// and the same `copyToClipboard` util + `errorCopy.copy`/`errorCopy.copied`
// i18n keys `leaf/Chips.tsx`'s FailureChip already reuses for the same
// purpose. "Sending..."/"Failed"/"Retry" are hardcoded, not i18n'd —
// legacy never translated these either (MessageBubble.tsx's own `config`
// object and Retry button are plain string literals), so hardcoding here
// preserves byte-for-byte parity rather than inventing new locale keys.

import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ProvisionalSend } from "../state";
import Icon from "../../components/Icon";
import { copyToClipboard } from "../../utils/clipboard";

export function PendingSendStatus({
  state,
  onRetry,
}: {
  state: ProvisionalSend;
  onRetry?: () => void;
}) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);
  const [copyPhase, setCopyPhase] = useState<"idle" | "copying" | "copied">("idle");
  const copyResetRef = useRef<number | null>(null);
  useEffect(() => () => {
    if (copyResetRef.current !== null) window.clearTimeout(copyResetRef.current);
  }, []);

  if (state.status === "sending") {
    return (
      <div className="message-status status-sending" data-testid="surface-prompt-status">
        <span className="status-dot" />
        <span>Sending...</span>
      </div>
    );
  }

  const errorText = state.errorMessage;
  const copyError = async () => {
    if (!errorText || copyPhase === "copying") return;
    if (copyResetRef.current !== null) {
      window.clearTimeout(copyResetRef.current);
      copyResetRef.current = null;
    }
    setCopyPhase("copying");
    if (!(await copyToClipboard(errorText))) {
      setCopyPhase("idle");
      return;
    }
    setCopyPhase("copied");
    copyResetRef.current = window.setTimeout(() => {
      setCopyPhase("idle");
      copyResetRef.current = null;
    }, 1600);
  };

  return (
    <div className="message-status status-error" data-testid="surface-prompt-status">
      <div className="error-block-header">
        <button
          type="button"
          className="error-block-toggle"
          onClick={(e) => {
            e.stopPropagation();
            if (errorText) setExpanded((v) => !v);
          }}
          aria-expanded={errorText ? expanded : undefined}
          disabled={!errorText}
        >
          <span className="status-dot" />
          <span className="error-block-label">Failed</span>
          {errorText && (
            <span className="collapse-arrow" aria-hidden="true">
              {expanded ? "▼" : "▶"}
            </span>
          )}
        </button>
        {errorText && (
          <button
            type="button"
            className={`error-copy-btn${copyPhase === "copied" ? " copied" : ""}`}
            onClick={(e) => {
              e.stopPropagation();
              void copyError();
            }}
            disabled={copyPhase === "copying"}
            aria-label={copyPhase === "copied" ? t("errorCopy.copied") : t("errorCopy.copy")}
            title={copyPhase === "copied" ? t("errorCopy.copied") : t("errorCopy.copy")}
          >
            <Icon name={copyPhase === "copied" ? "check" : "clipboard"} size={13} />
          </button>
        )}
        {onRetry && (
          <button
            type="button"
            className="status-retry-btn"
            onClick={(e) => {
              e.stopPropagation();
              onRetry();
            }}
          >
            Retry
          </button>
        )}
      </div>
      {expanded && errorText && <pre className="error-block-body">{errorText}</pre>}
    </div>
  );
}
