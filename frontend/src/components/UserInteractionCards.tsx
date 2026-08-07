// Presentational + submit "core" for the three pending-user-interaction
// card kinds (input / approval / memory-proposal) — extracted so both the
// legacy ChatMessage-shaped call sites (components/Chat.tsx's
// UserInputCard/UserApprovalCard/MemoryProposalCard, which stay as thin
// field-extracting wrappers around these) and the native Contract-Node
// layer (surface/nodes/UserInteractionAction.tsx, which has no
// UserInteractionRequest/ChatMessage types to draw from) can render the
// SAME interactive form + hit the SAME resolution endpoints, with zero
// duplication of the fetch/validation logic. Every prop here is a plain
// value — no legacy request-union coupling.

import { useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { API } from "../api";

export interface UserInputCardCoreOption {
  label: string;
  description?: string;
}

export interface UserInputCardCoreQuestion {
  id: string;
  header: string;
  question: string;
  options?: UserInputCardCoreOption[];
}

export function UserInputCardCore({
  requestId,
  appSessionId,
  questions,
  onDone,
}: {
  requestId: string;
  appSessionId: string;
  questions: UserInputCardCoreQuestion[];
  onDone: (requestId: string) => void;
}) {
  const { t } = useTranslation();
  const [answers, setAnswers] = useState<Record<string, string>>(() => {
    const initial: Record<string, string> = {};
    for (const q of questions) {
      initial[q.id] = q.options?.[0]?.label ?? "";
    }
    return initial;
  });
  const [submitting, setSubmitting] = useState(false);
  const textRefs = useRef<Record<string, HTMLInputElement | null>>({});
  const canSubmit = questions.every((q) => (answers[q.id] || "").trim());

  const pickOption = (questionId: string, label: string) => {
    setAnswers((prev) => ({ ...prev, [questionId]: label }));
    const el = textRefs.current[questionId];
    if (el) requestAnimationFrame(() => { el.focus(); el.select(); });
  };

  const submit = async () => {
    if (!canSubmit || submitting) return;
    setSubmitting(true);
    try {
      const res = await fetch(`${API}/api/user-input/${encodeURIComponent(requestId)}/resolve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ app_session_id: appSessionId, answers }),
      });
      if (res.ok) onDone(requestId);
    } finally {
      setSubmitting(false);
    }
  };

  const cancel = async () => {
    if (submitting) return;
    setSubmitting(true);
    try {
      const res = await fetch(`${API}/api/user-input/${encodeURIComponent(requestId)}/cancel`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ app_session_id: appSessionId }),
      });
      if (res.ok) onDone(requestId);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="user-input-card" data-testid="user-input-card">
      <div className="user-input-card__title">{t("userInput.title")}</div>
      {questions.map((q) => (
        <div className="user-input-card__question" key={q.id}>
          <div className="user-input-card__header">{q.header}</div>
          <div className="user-input-card__body">{q.question}</div>
          {q.options && q.options.length > 0 ? (
            <div className="user-input-card__options">
              {q.options.map((option) => (
                <label className="user-input-card__option" key={option.label}>
                  <input
                    type="radio"
                    name={`${requestId}:${q.id}`}
                    checked={answers[q.id] === option.label}
                    onChange={() => pickOption(q.id, option.label)}
                    disabled={submitting}
                  />
                  <span>
                    <strong>{option.label}</strong>
                    {option.description ? <small>{option.description}</small> : null}
                  </span>
                </label>
              ))}
              <input
                ref={(el) => { textRefs.current[q.id] = el; }}
                className="user-input-card__text"
                value={answers[q.id] ?? ""}
                onChange={(e) => setAnswers((prev) => ({ ...prev, [q.id]: e.target.value }))}
                onFocus={(e) => e.target.select()}
                disabled={submitting}
                placeholder={t("userInput.otherAnswer")}
              />
            </div>
          ) : (
            <textarea
              className="user-input-card__textarea"
              value={answers[q.id] ?? ""}
              onChange={(e) => setAnswers((prev) => ({ ...prev, [q.id]: e.target.value }))}
              disabled={submitting}
              rows={3}
            />
          )}
        </div>
      ))}
      <div className="user-input-card__actions">
        <button type="button" onClick={cancel} disabled={submitting}>{t("userInput.cancel")}</button>
        <button type="button" className="primary" onClick={submit} disabled={!canSubmit || submitting}>
          {submitting ? t("userInput.submitting") : t("userInput.send")}
        </button>
      </div>
    </div>
  );
}

export function UserApprovalCardCore({
  requestId,
  appSessionId,
  prompt,
  onDone,
}: {
  requestId: string;
  appSessionId: string;
  prompt: string;
  onDone: (requestId: string) => void;
}) {
  const { t } = useTranslation();
  const [alternative, setAlternative] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [failed, setFailed] = useState(false);

  const resolve = async (approved: boolean) => {
    const text = alternative.trim();
    if (submitting || (!approved && !text)) return;
    setSubmitting(true);
    setFailed(false);
    try {
      const res = await fetch(`${API}/api/user-input/${encodeURIComponent(requestId)}/resolve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({
          app_session_id: appSessionId,
          approved,
          ...(approved ? {} : { alternative: text }),
        }),
      });
      if (res.ok) {
        onDone(requestId);
        return;
      }
      setFailed(true);
    } catch {
      setFailed(true);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="user-input-card user-approval-card" data-testid="user-approval-card">
      <div className="user-input-card__title">{t("userApproval.title")}</div>
      <div className="user-input-card__body">{prompt}</div>
      <textarea
        className="user-input-card__textarea"
        data-action="alternative"
        value={alternative}
        onChange={(event) => setAlternative(event.target.value)}
        placeholder={t("userApproval.alternativePlaceholder")}
        disabled={submitting}
        rows={3}
      />
      {failed ? <div className="user-approval-card__error" role="alert">{t("userApproval.failed")}</div> : null}
      <div className="user-input-card__actions">
        <button
          type="button"
          data-action="submit-alternative"
          onClick={() => resolve(false)}
          disabled={submitting || !alternative.trim()}
        >
          {submitting ? t("userApproval.submitting") : t("userApproval.useAlternative")}
        </button>
        <button
          type="button"
          className="primary"
          data-action="approve"
          onClick={() => resolve(true)}
          disabled={submitting}
        >
          {submitting ? t("userApproval.submitting") : t("userApproval.approve")}
        </button>
      </div>
    </div>
  );
}

const MEMORY_TYPES = ["user", "feedback", "project", "reference"] as const;
const MEMORY_SCOPE_TYPES = ["global", "project", "folder"] as const;

export type MemoryProposalCardCoreType = (typeof MEMORY_TYPES)[number];
export type MemoryProposalCardCoreScopeType = (typeof MEMORY_SCOPE_TYPES)[number];

export interface MemoryProposalCardCoreFields {
  action: "add" | "edit";
  name: string;
  description: string;
  type: MemoryProposalCardCoreType;
  content: string;
  scope_type: MemoryProposalCardCoreScopeType;
  scope_path: string;
  target_slug?: string;
}

export function MemoryProposalCardCore({
  requestId,
  appSessionId,
  memoryProposal,
  onDone,
}: {
  requestId: string;
  appSessionId: string;
  memoryProposal: MemoryProposalCardCoreFields;
  onDone: (requestId: string) => void;
}) {
  const { t } = useTranslation();
  const [fields, setFields] = useState<MemoryProposalCardCoreFields>(() => ({ ...memoryProposal }));
  const [expanded, setExpanded] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [failed, setFailed] = useState(false);
  const canSubmit = fields.name.trim() && fields.description.trim() && fields.content.trim()
    && (fields.scope_type === "global" || fields.scope_path.trim());

  const set = <K extends keyof MemoryProposalCardCoreFields>(key: K, value: MemoryProposalCardCoreFields[K]) =>
    setFields((prev) => ({ ...prev, [key]: value }));

  const resolve = async (approved: boolean) => {
    if (submitting) return;
    if (approved && !canSubmit) return;
    setSubmitting(true);
    setFailed(false);
    try {
      const res = await fetch(`${API}/api/user-input/${encodeURIComponent(requestId)}/resolve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({
          app_session_id: appSessionId,
          approved,
          ...(approved ? { edited: fields } : {}),
        }),
      });
      if (res.ok) {
        onDone(requestId);
        return;
      }
      setFailed(true);
    } catch {
      setFailed(true);
    } finally {
      setSubmitting(false);
    }
  };

  const fieldsBody = (
    <>
      <label className="memory-proposal-card__field">
        <span>{t("memoryProposal.name")}</span>
        <input
          value={fields.name}
          onChange={(e) => set("name", e.target.value)}
          disabled={submitting}
        />
      </label>
      <label className="memory-proposal-card__field">
        <span>{t("memoryProposal.description")}</span>
        <input
          value={fields.description}
          onChange={(e) => set("description", e.target.value)}
          disabled={submitting}
        />
      </label>
      <div className="memory-proposal-card__row">
        <label className="memory-proposal-card__field">
          <span>{t("memoryProposal.type")}</span>
          <select value={fields.type} onChange={(e) => set("type", e.target.value as MemoryProposalCardCoreType)} disabled={submitting}>
            {MEMORY_TYPES.map((type) => (
              <option key={type} value={type}>{t(`memoryProposal.type_${type}`)}</option>
            ))}
          </select>
        </label>
        <label className="memory-proposal-card__field">
          <span>{t("memoryProposal.scope")}</span>
          <select
            value={fields.scope_type}
            onChange={(e) => set("scope_type", e.target.value as MemoryProposalCardCoreScopeType)}
            disabled={submitting}
          >
            {MEMORY_SCOPE_TYPES.map((scope) => (
              <option key={scope} value={scope}>{t(`memoryProposal.scope_${scope}`)}</option>
            ))}
          </select>
        </label>
      </div>
      {fields.scope_type !== "global" ? (
        <label className="memory-proposal-card__field">
          <span>{t("memoryProposal.scopePath")}</span>
          <input
            value={fields.scope_path}
            onChange={(e) => set("scope_path", e.target.value)}
            disabled={submitting}
            placeholder="/abs/path"
          />
        </label>
      ) : null}
      <label className="memory-proposal-card__field">
        <span>{t("memoryProposal.content")}</span>
        <textarea
          className="memory-proposal-card__content"
          value={fields.content}
          onChange={(e) => set("content", e.target.value)}
          disabled={submitting}
          rows={expanded ? 18 : 4}
        />
      </label>
    </>
  );

  return (
    <div className="user-input-card memory-proposal-card" data-testid="memory-proposal-card">
      <div className="user-input-card__title">
        {fields.action === "edit" ? t("memoryProposal.titleEdit") : t("memoryProposal.titleAdd")}
      </div>
      {fieldsBody}
      {failed ? <div className="user-approval-card__error" role="alert">{t("memoryProposal.failed")}</div> : null}
      <div className="user-input-card__actions">
        <button type="button" onClick={() => setExpanded(true)} disabled={submitting}>
          {t("memoryProposal.expand")}
        </button>
        <button type="button" onClick={() => resolve(false)} disabled={submitting}>
          {t("memoryProposal.reject")}
        </button>
        <button type="button" className="primary" onClick={() => resolve(true)} disabled={submitting || !canSubmit}>
          {submitting ? t("memoryProposal.submitting") : t("memoryProposal.approve")}
        </button>
      </div>
      {expanded ? (
        <div className="memory-proposal-modal__overlay" onClick={() => setExpanded(false)}>
          <div className="memory-proposal-modal" onClick={(e) => e.stopPropagation()}>
            <div className="memory-proposal-modal__title">
              {fields.action === "edit" ? t("memoryProposal.titleEdit") : t("memoryProposal.titleAdd")}
            </div>
            {fieldsBody}
            {failed ? <div className="user-approval-card__error" role="alert">{t("memoryProposal.failed")}</div> : null}
            <div className="user-input-card__actions">
              <button type="button" onClick={() => setExpanded(false)} disabled={submitting}>
                {t("memoryProposal.collapse")}
              </button>
              <button type="button" onClick={() => resolve(false)} disabled={submitting}>
                {t("memoryProposal.reject")}
              </button>
              <button type="button" className="primary" onClick={() => resolve(true)} disabled={submitting || !canSubmit}>
                {submitting ? t("memoryProposal.submitting") : t("memoryProposal.approve")}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
