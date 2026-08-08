// Compact chip/box leaves — native-typed equivalents of MessageBubble.tsx's
// FailureChip/FactChipEvent/PrLinkEvent/UserInteractionNotice/
// DiagnosticEvent, taking the actual wire.ts payload types directly
// (no `Record<string, unknown>` re-parsing — that was only ever needed
// because the legacy path down-mapped through generic WSEvent.data).

import { useEffect, useRef, useState, type CSSProperties } from "react";
import { useTranslation } from "react-i18next";
import type {
  FactPayloadWire,
  FailurePayloadWire,
  UserInteractionPayloadWire,
} from "../../adapter/wire";
import Icon from "../../components/Icon";
import { copyToClipboard } from "../../utils/clipboard";
import { RateLimitBox } from "./Pills";

const FAILURE_SEVERITY_STYLE: Record<string, { color: string; background: string }> = {
  info: { color: "var(--text-muted)", background: "rgba(139,148,158,0.16)" },
  warning: { color: "var(--warning, #d29922)", background: "rgba(210,153,34,0.16)" },
  error: { color: "var(--danger, #f85149)", background: "rgba(248,81,73,0.16)" },
};

const chipBoxStyle: CSSProperties = {
  fontSize: "0.8em",
  opacity: 0.85,
  border: "1px dashed currentColor",
  padding: "4px 8px",
  margin: "4px 0",
  borderRadius: 4,
  display: "flex",
  alignItems: "center",
  gap: 6,
  flexWrap: "wrap",
};

/** `failure` node — full taxonomy per backend/surface_contract/nodes.py.
 * `fix_credential`/`open_settings` resolutions don't have a native-layer
 * settings-deep-link wired yet (that affordance lives in the legacy
 * CredentialErrorFix component, which takes ChatMessage-shaped props) —
 * falls through to the generic chip with its text, a known stage-1 gap.
 *
 * Copy + expand/disclosure chrome mirrors legacy MessageBubble.tsx's
 * `MessageStatus` error block 1:1 (`.error-block-toggle`/`.collapse-arrow`/
 * `.error-copy-btn`/`.error-block-body`, `errorCopy.copy`/`errorCopy.copied`
 * i18n keys — all pre-existing, no new CSS/locale strings needed): the
 * header toggles a `<pre>` disclosure of the full failure text, and a
 * separate button copies it via the same `copyToClipboard` util. */
export function FailureChip({ payload }: { payload: FailurePayloadWire }) {
  const { t } = useTranslation();
  const { code, text, severity, retryable, resolution } = payload;
  const label = t("message.failureChip", { code, defaultValue: text || code });
  const [expanded, setExpanded] = useState(false);
  const [copyPhase, setCopyPhase] = useState<"idle" | "copying" | "copied">("idle");
  const copyResetRef = useRef<number | null>(null);
  useEffect(() => () => {
    if (copyResetRef.current !== null) window.clearTimeout(copyResetRef.current);
  }, []);

  if (resolution === "choose_fallback") {
    return <RateLimitBox label={label} errorText={text} />;
  }

  const palette = FAILURE_SEVERITY_STYLE[severity] ?? FAILURE_SEVERITY_STYLE.error;
  const copyError = async () => {
    if (!text || copyPhase === "copying") return;
    if (copyResetRef.current !== null) {
      window.clearTimeout(copyResetRef.current);
      copyResetRef.current = null;
    }
    setCopyPhase("copying");
    if (!(await copyToClipboard(text))) {
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
    <div className="event-diagnostic" style={chipBoxStyle} data-testid="surface-failure-chip">
      <button
        type="button"
        className="error-block-toggle"
        onClick={() => text && setExpanded((v) => !v)}
        aria-expanded={text ? expanded : undefined}
        disabled={!text}
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          background: "none",
          border: "none",
          padding: 0,
          color: "inherit",
          font: "inherit",
          cursor: text ? "pointer" : "default",
        }}
      >
        <span
          style={{
            padding: "1px 7px",
            borderRadius: 999,
            fontSize: "0.9em",
            fontWeight: 600,
            background: palette.background,
            color: palette.color,
          }}
        >
          {t(`message.failureSeverity_${severity}`, { defaultValue: severity })}
        </span>
        <span>{label}</span>
        {retryable && <span style={{ opacity: 0.7 }}>{t("message.failureRetryable", { defaultValue: "Retryable" })}</span>}
        {text && (
          <span className="collapse-arrow" aria-hidden="true">
            {expanded ? "▼" : "▶"}
          </span>
        )}
      </button>
      {text && (
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
      {expanded && text && (
        <pre className="error-block-body" style={{ flexBasis: "100%", margin: 0 }}>
          {text}
        </pre>
      )}
    </div>
  );
}

/** `fact` node, `kind: "pr_link"` variant — the same recognizable
 * PR-link pill legacy renders. */
export function PrLinkChip({ data }: { data: Record<string, unknown> }) {
  const prUrl = typeof data.prUrl === "string" ? data.prUrl : undefined;
  const prNumber = typeof data.prNumber === "number" ? data.prNumber : undefined;
  const prRepository = typeof data.prRepository === "string" ? data.prRepository : undefined;
  if (!prUrl) return null;
  const label = prNumber ? `Pull request #${prNumber}` : "Pull request";
  return (
    <a className="event-pr-link" href={prUrl} target="_blank" rel="noopener noreferrer" title={prUrl}>
      <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true" style={{ flexShrink: 0 }}>
        <path d="M3.25 1A2.25 2.25 0 0 0 2.5 5.372V10.628a2.25 2.25 0 1 0 1.5 0V5.372A2.25 2.25 0 0 0 3.25 1Zm0 1.5a.75.75 0 1 1 0 1.5.75.75 0 0 1 0-1.5Zm0 9.25a.75.75 0 1 1 0 1.5.75.75 0 0 1 0-1.5ZM12.75 3a.75.75 0 1 0 0 1.5.75.75 0 0 0 0-1.5Zm-2.25.75a2.25 2.25 0 1 1 3 2.122v4.756a2.25 2.25 0 1 1-1.5 0V5.872A2.25 2.25 0 0 1 10.5 3.75Zm2.25 8a.75.75 0 1 0 0 1.5.75.75 0 0 0 0-1.5Z" />
      </svg>
      <span className="event-pr-link-label">{label}</span>
      {prRepository && <span className="event-pr-link-repo">{prRepository}</span>}
    </a>
  );
}

/** `fact` node — every kind other than `pr_link`, which has its own
 * dedicated PrLinkChip. */
export function FactChip({ payload }: { payload: FactPayloadWire }) {
  const { t } = useTranslation();
  if (payload.kind === "pr_link") return <PrLinkChip data={payload.data} />;
  const entries = Object.entries(payload.data ?? {});
  const summary = entries
    .map(([key, value]) => `${key}: ${typeof value === "string" ? value : JSON.stringify(value)}`)
    .join(", ");
  return (
    <div className="event-diagnostic" style={{ ...chipBoxStyle, opacity: 0.7 }} data-testid="surface-fact-chip">
      <span>{t("message.factChip", { kind: payload.kind })}</span>
      {summary ? <span style={{ marginInlineStart: 6, opacity: 0.85 }}>{summary}</span> : null}
    </div>
  );
}

/** `user_interaction` node — read-only state chip, for a resolved/
 * cancelled node or an unrecognized `kind`. A PENDING node whose `kind`
 * is input/approval/memory instead renders the real action form — see
 * nodes/UserInteractionAction.tsx, which is the only caller for that
 * case; this chip is its fallback. */
export function UserInteractionChip({ payload }: { payload: UserInteractionPayloadWire }) {
  const { t } = useTranslation();
  const { kind, state } = payload;
  const stateColor =
    state === "pending" ? "var(--warning, #d29922)" : state === "resolved" ? "var(--success, #3fb950)" : "var(--text-muted)";
  const stateBackground =
    state === "pending" ? "rgba(210,153,34,0.16)" : state === "resolved" ? "rgba(63,185,80,0.16)" : "rgba(139,148,158,0.16)";
  return (
    <div className="event-diagnostic" style={{ ...chipBoxStyle, opacity: 0.7 }} data-testid="surface-user-interaction-chip">
      <span>{t("message.userInteraction", { kind })}</span>
      <span
        style={{
          padding: "1px 7px",
          borderRadius: 999,
          fontSize: "0.9em",
          fontWeight: 600,
          background: stateBackground,
          color: stateColor,
        }}
      >
        {t(`message.userInteractionState_${state}`, { defaultValue: state })}
      </span>
    </div>
  );
}

/** `unknown` node, and the fallback for a `diagnostic` node's own
 * unrecognized `code` — a raw-JSON disclosure box, same visual language
 * as legacy's DiagnosticEvent. */
export function DiagnosticBox({ kind, raw }: { kind: string; raw: unknown }) {
  const [open, setOpen] = useState(false);
  let pretty: string;
  try {
    pretty = JSON.stringify(raw, null, 2);
  } catch {
    pretty = String(raw);
  }
  return (
    <div className="event-diagnostic" style={{ fontSize: "0.8em", opacity: 0.7, border: "1px dashed currentColor", padding: "4px 8px", margin: "4px 0", borderRadius: 4 }} data-testid="surface-diagnostic-box">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        style={{ background: "none", border: "none", cursor: "pointer", padding: 0, color: "inherit", font: "inherit" }}
      >
        {open ? "▾" : "▸"} unknown: <code>{kind}</code>
      </button>
      {open && (
        <pre style={{ marginTop: 4, whiteSpace: "pre-wrap", wordBreak: "break-word", maxHeight: 200, overflow: "auto" }}>
          {pretty}
        </pre>
      )}
    </div>
  );
}
