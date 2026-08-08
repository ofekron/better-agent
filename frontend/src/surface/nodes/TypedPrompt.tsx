// `typed_prompt` node — chat-panel.md's TypedPrompt: renders exactly what
// `origin` supplied, verbatim (ADR 0006 §0 "Prompt fidelity"), with ONE
// exception — an artificial-section preamble (a `<system-reminder>`,
// `<verdict-prompt>`, `<inline-tags>`, … wrapper the backend or the
// frontend send-path injects around contextual content) renders as
// collapsible chips instead of raw XML, at parity with legacy
// MessageBubble.tsx's `UserContentSegments`/`ArtificialSectionChip`.
// Delegates entirely to `leaf/ArtificialSections.tsx` — the ONE shared
// native renderer for this (also used by SteeringMessageView) — rather
// than forking a second, prompt-specific parser. `sent_text` is on-demand
// only (full-prompt affordance), never inline by default.
//
// Origin labeling (`payload.origin`) is at parity with legacy's
// `src/lib/turnMessageHeader.ts` + `SyntheticPromptChip` (MessageBubble.tsx)
// for every origin the native `PromptOriginWire` enum actually carries:
//   - "supervisor": rendered as the SAME collapsible chip legacy used
//     (`.synthetic-prompt-chip`/`.synthetic-prompt-supervisor` CSS,
//     hardcoded "Supervisor prompt" label — legacy never i18n'd this
//     string either, so hardcoding here preserves byte-for-byte parity).
//   - "ask": legacy's `team_ask`/`mssg` sources collapsed into one native
//     origin — rendered with the same `.message-box-header
//     .standalone-user-source` icon+label header legacy used for
//     "Ask"/"Message", plus the same `.team-message-from` "FROM <link>"
//     row when `source_session_ref` is set (backend only ever sets it for
//     this origin — surface_contract/nodes.py's own field comment).
//   - "offline_sync": no legacy source string mapped to this — the
//     backend/native protocol is what introduced tracking it explicitly.
//     Given its own header, new i18n copy (no legacy text to copy).
//   - "queued": intentionally NOT given a header — the backend sets this
//     origin 1:1 with `status: "queued"` (adapters/normalize.py), already
//     surfaced below via the `surface-prompt-queued` badge; a second label
//     would be redundant.
//   - "user" (the plain, source-less case): renders the SAME icon+label
//     header legacy's grouped `TurnGroupImpl` always shows for its turn
//     initiator (`turnMessageHeader.ts` with no `source` — icon "👤",
//     label = the configured `userDisplayName`, "User" default), threaded
//     down from Chat.tsx via TurnView/ChatSurfaceView now that this
//     component's props reach that far. Uses `.message-box-icon`/
//     `.message-box-label` WITHOUT the "orchestration-" modifier classes,
//     matching legacy's own conditional styling (those classes are only
//     added when `initiatorMessage.source` is truthy — never true for a
//     plain user turn).
//
// A `TurnEntry.provisionalSend` (state.ts) is a client-only send-in-flight
// marker, never a wire field — `PendingSendStatus` (leaf/) renders its
// sending/failed+retry chrome as an extra child, at parity with legacy's
// `MessageStatus` block on an optimistic pending bubble.

import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { NodeWire, PromptOriginWire, TypedPromptPayloadWire } from "../../adapter/wire";
import type { ProvisionalSend } from "../state";
import { Markdown } from "../leaf/Markdown";
import { CollapsibleBlock } from "../leaf/Collapsible";
import { ArtificialSectionSegments } from "../leaf/ArtificialSections";
import { AttachmentChips } from "../leaf/AttachmentChips";
import { PendingSendStatus } from "../leaf/PendingSendStatus";
import { linkifyFilePaths, sessionLinkMarker } from "../../utils/linkifyFilePaths";
import { isSaveShortcutEvent } from "../../hooks/useSaveShortcut";
import { Timestamp } from "../leaf/Timestamp";
import Icon from "../../components/Icon";

const ORIGIN_HEADER: Partial<Record<PromptOriginWire, { icon: string; labelKey: string }>> = {
  ask: { icon: "✉", labelKey: "message.promptOriginAsk" },
  offline_sync: { icon: "⏳", labelKey: "message.promptOriginOfflineSync" },
};

/** "FROM <session-link>" row — same shape/classes as legacy's
 * `TeamMessageFrom`, minus a separate display name (the wire only ever
 * gives a bare session ref, no `sender_name`). */
function FromSender({ sourceSessionRef }: { sourceSessionRef: string }) {
  const { t } = useTranslation();
  return (
    <div className="team-message-from">
      <span className="team-message-from-label">{t("message.fromSender")}</span>
      {linkifyFilePaths(sessionLinkMarker(sourceSessionRef, sourceSessionRef))}
    </div>
  );
}

function OriginHeader({
  origin,
  sourceSessionRef,
}: {
  origin: PromptOriginWire;
  sourceSessionRef: string | null;
}) {
  const { t } = useTranslation();
  const spec = ORIGIN_HEADER[origin];
  if (!spec) return null;
  return (
    <div className="message-box-header standalone-user-source">
      <span className="message-box-icon orchestration-icon">{spec.icon}</span>
      <span className="message-box-label orchestration-label">{t(spec.labelKey)}</span>
      {sourceSessionRef && <FromSender sourceSessionRef={sourceSessionRef} />}
    </div>
  );
}

/** `origin: "user"` header — same content/classes as legacy's grouped
 * turn-initiator header (`turnMessageHeader(undefined, userDisplayName)`),
 * always shown (not conditional the way the "ask"/"offline_sync" header
 * above is), matching `TurnGroupImpl`'s unconditional rendering for its
 * own initiator row. */
function PlainUserHeader({ userDisplayName }: { userDisplayName?: string | null }) {
  const label = (userDisplayName ?? "").trim().replace(/\s+/g, " ") || "User";
  return (
    <div className="message-box-header standalone-user-source">
      <span className="message-box-icon">👤</span>
      <span className="message-box-label">{label}</span>
    </div>
  );
}

/** Alter (edit-and-resend) trigger — legacy's `.message-header-actions`/
 * `.alter-user-message-btn` (`MessageBubble.tsx`), shown in the header row
 * alongside the origin header. Gated by the caller (TurnView.tsx) to the
 * session's latest, non-provisional turn exactly as legacy's
 * `onAlterTurnMessage` was (Chat.tsx: `g.isLatest && !pending`). */
function AlterTriggerButton({ onClick }: { onClick: () => void }) {
  const { t } = useTranslation();
  return (
    <div className="message-header-actions">
      <button
        type="button"
        className="message-header-action-btn alter-user-message-btn"
        onClick={onClick}
        title={t("message.alterTitle")}
      >
        <Icon name="edit" size={13} />
        <span>{t("message.alterButton")}</span>
      </button>
    </div>
  );
}

/** Inline edit-and-resend editor — legacy's `.alter-user-message-editor`/
 * `-textarea`/`-actions`/`-cancel`/`-save` (`MessageBubble.tsx`), replacing
 * the normal prompt body while active, same as legacy's
 * `isEditingInitiator` branch. Dispatches through `onAlter`, which
 * TurnView.tsx wires to the SAME generic `SurfaceStore.sendPrompt` every
 * other send goes through, stamped `send_mode: "alter"` (backend
 * `SendMode.ALTER` — an atomic rewind-latest-user-and-resend, or an
 * in-place queued update when the target prompt is still queued) — no
 * attachment carryover, matching legacy's own text-only alter
 * (`handleAlterUserMessage` never threads the original images/files
 * through). */
function AlterEditor({
  text,
  onAlter,
  onDone,
}: {
  text: string;
  onAlter: (text: string) => void;
  onDone: () => void;
}) {
  const { t } = useTranslation();
  const [draft, setDraft] = useState(text);

  const submit = () => {
    const next = draft.trim();
    if (next && next !== text.trim()) onAlter(next);
    onDone();
  };

  return (
    <div className="alter-user-message-editor" data-testid="surface-prompt-alter-editor">
      <textarea
        className="alter-user-message-textarea"
        value={draft}
        rows={Math.min(10, Math.max(3, draft.split("\n").length))}
        onChange={(event) => setDraft(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            onDone();
            return;
          }
          if ((event.key === "Enter" && (event.metaKey || event.ctrlKey)) || isSaveShortcutEvent(event)) {
            event.preventDefault();
            submit();
          }
        }}
      />
      <div className="alter-user-message-actions">
        <button type="button" className="alter-user-message-cancel" onClick={onDone}>
          {t("message.cancelAlterButton")}
        </button>
        <button type="button" className="alter-user-message-save" onClick={submit} disabled={!draft.trim()}>
          {t("message.saveAlterButton")}
        </button>
      </div>
    </div>
  );
}

/** First non-empty line, trimmed — same truncation rule as legacy's
 * `SyntheticPromptChip` preview. */
function firstLinePreview(text: string, max = 96): string {
  const firstLine = text.split("\n", 1)[0] ?? "";
  return firstLine.length > max ? firstLine.slice(0, max - 1) + "…" : firstLine;
}

/** `origin: "supervisor"` — a programmatic adversarial prompt containing
 * embedded worker output as context, verbose and confusing as a full
 * prompt. Rendered as the SAME collapsible chip legacy's
 * `SyntheticPromptChip` used (`.synthetic-prompt-chip`/
 * `.synthetic-prompt-supervisor`/`.synthetic-prompt-header`/
 * `.synthetic-prompt-caret`/`.synthetic-prompt-label`/
 * `.synthetic-prompt-preview`/`.synthetic-prompt-body` — all pre-existing
 * CSS, no new rules needed), collapsed by default so the response stays
 * the visual focus. */
function SupervisorPromptChip({ text }: { text: string }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div className={`message synthetic-prompt-chip synthetic-prompt-supervisor${expanded ? " expanded" : ""}`}>
      <button
        type="button"
        className="synthetic-prompt-header"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
      >
        <span className="synthetic-prompt-caret" aria-hidden="true">{expanded ? "▾" : "▸"}</span>
        <span className="synthetic-prompt-label">Supervisor prompt</span>
        {!expanded && <span className="synthetic-prompt-preview">{firstLinePreview(text)}</span>}
      </button>
      {expanded && (
        <div className="synthetic-prompt-body">
          <Markdown text={text} />
        </div>
      )}
    </div>
  );
}

/** `sent_text` — the actually-dispatched prompt when harness wrapping
 * diverges from the originator's verbatim `text` (backend
 * TypedPromptPayload's own field comment). No legacy analogue to port
 * (legacy never wrapped/diverged prompt text client-side) — a net-new
 * on-demand affordance, using the shared generic `CollapsibleBlock`
 * primitive rather than forking a new disclosure pattern. Never rendered
 * when identical to `text` (nothing diverged, nothing to disclose). */
function SentTextDisclosure({ sentText, text }: { sentText: string; text: string }) {
  const { t } = useTranslation();
  if (sentText === text) return null;
  return (
    <CollapsibleBlock
      className="surface-prompt-sent-text"
      testId="surface-prompt-sent-text"
      header={<span>{t("message.promptSentTextLabel")}</span>}
    >
      <Markdown text={sentText} />
    </CollapsibleBlock>
  );
}

export function TypedPromptView({
  node,
  userDisplayName,
  provisionalSend,
  onRetrySend,
  onAlter,
}: {
  node: NodeWire;
  /** Only meaningful for `payload.origin === "user"` — see
   * `PlainUserHeader`. Omitted by generic dispatch call sites
   * (NodeView.tsx, reached for a nested typed_prompt e.g. inside a
   * SubAgentTurn body) where no plain-user prompt is structurally
   * expected; `PlainUserHeader` falls back to the "User" default there. */
  userDisplayName?: string | null;
  provisionalSend?: ProvisionalSend | null;
  onRetrySend?: () => void;
  /** Present only when TurnView.tsx has gated this prompt's turn as
   * alterable (the session's latest, non-provisional turn — legacy's
   * `onAlterTurnMessage`/`g.isLatest && !pending` rule). Undefined hides
   * the Alter affordance entirely, matching legacy's own
   * `onAlterTurnMessage &&` conditional. */
  onAlter?: (text: string) => void;
}) {
  const { t } = useTranslation();
  const payload = node.payload as TypedPromptPayloadWire | null;
  const [altering, setAltering] = useState(false);
  if (!payload) return null;

  if (payload.origin === "supervisor") {
    return (
      <div
        className="surface-prompt message-box user-message-box"
        data-testid="surface-typed-prompt"
        data-origin={payload.origin}
      >
        {altering && onAlter ? (
          <AlterEditor text={payload.text} onAlter={onAlter} onDone={() => setAltering(false)} />
        ) : (
          <>
            <SupervisorPromptChip text={payload.text} />
            {onAlter && <AlterTriggerButton onClick={() => setAltering(true)} />}
          </>
        )}
        {provisionalSend && <PendingSendStatus state={provisionalSend} onRetry={onRetrySend} />}
        <Timestamp ts={node.ts} />
      </div>
    );
  }

  return (
    <div
      className="surface-prompt message-box user-message-box"
      data-testid="surface-typed-prompt"
      data-origin={payload.origin}
    >
      <div className="message-box-header-row">
        {payload.origin === "user" ? (
          <PlainUserHeader userDisplayName={userDisplayName} />
        ) : (
          <OriginHeader origin={payload.origin} sourceSessionRef={payload.source_session_ref} />
        )}
        {onAlter && !altering && <AlterTriggerButton onClick={() => setAltering(true)} />}
      </div>
      {altering && onAlter ? (
        <AlterEditor text={payload.text} onAlter={onAlter} onDone={() => setAltering(false)} />
      ) : (
        <>
          <ArtificialSectionSegments text={payload.text} />
          <AttachmentChips attachments={payload.attachments} sessionId={node.surface_id} />
        </>
      )}
      {node.status === "queued" ? <span className="surface-prompt-queued">{t("input.queuedLabel")}</span> : null}
      {payload.sent_text && <SentTextDisclosure sentText={payload.sent_text} text={payload.text} />}
      {provisionalSend && <PendingSendStatus state={provisionalSend} onRetry={onRetrySend} />}
      <Timestamp ts={node.ts} />
    </div>
  );
}
