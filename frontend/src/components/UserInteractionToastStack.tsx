import { useState } from "react";
import { useTranslation } from "react-i18next";
import { sessionPath } from "../hooks/useRoute";
import type { Session, UserInteractionRequest } from "../types";
import { UserApprovalCard, UserInputCard } from "./Chat";

interface Props {
  requests: UserInteractionRequest[];
  sessions: Session[];
  onOpenSession: (sessionId: string) => void;
  onDismiss: (requestId: string) => void;
}

function requestSummary(request: UserInteractionRequest): string {
  if (request.kind === "approval") return request.prompt;
  return request.questions[0]?.question ?? "";
}

export function UserInteractionToastStack({
  requests,
  sessions,
  onOpenSession,
  onDismiss,
}: Props) {
  const { t } = useTranslation();
  const [expandedRequestId, setExpandedRequestId] = useState<string | null>(null);
  const sessionNames = new Map(sessions.map((session) => [session.id, session.name]));

  return requests.map((request) => {
    const sessionName = sessionNames.get(request.app_session_id);
    const expanded = expandedRequestId === request.request_id;
    return (
      <article
        className="user-request-toast"
        data-testid="user-request-toast"
        data-session-id={request.app_session_id}
        data-kind={request.kind}
        key={request.request_id}
        role="status"
      >
        <svg className="user-request-toast__icon" viewBox="0 0 24 24" aria-hidden="true">
          <path d="M12 3a8 8 0 0 0-8 8v1.5a3 3 0 0 1-.88 2.12L2 15.74V18h20v-2.26l-1.12-1.12A3 3 0 0 1 20 12.5V11a8 8 0 0 0-8-8Zm0 19a3 3 0 0 0 2.83-2h-5.66A3 3 0 0 0 12 22Z" />
        </svg>
        <div className="user-request-toast__content">
          <strong className="user-request-toast__title">
            {request.kind === "approval" ? t("userApproval.title") : t("userInput.title")}
          </strong>
          <span className="user-request-toast__summary">{requestSummary(request)}</span>
          {sessionName ? <span className="user-request-toast__session">{sessionName}</span> : null}
          <div className="user-request-toast__actions">
            <button
              className="user-request-toast__respond"
              data-action="respond-in-place"
              type="button"
              aria-expanded={expanded}
              onClick={() => setExpandedRequestId(expanded ? null : request.request_id)}
            >
              {expanded ? t("userRequest.collapse") : t("userRequest.respond")}
            </button>
            <a
              className="user-request-toast__link"
              data-action="open-session"
              href={sessionPath(request.app_session_id)}
              onClick={(event) => {
                event.preventDefault();
                onOpenSession(request.app_session_id);
              }}
            >
              {t("userRequest.openSession")}
            </a>
          </div>
          <div className="user-request-toast__response" hidden={!expanded}>
            {request.kind === "approval" ? (
              <UserApprovalCard request={request} onDone={onDismiss} />
            ) : (
              <UserInputCard request={request} onDone={onDismiss} />
            )}
          </div>
        </div>
        <button
          className="user-request-toast__close"
          type="button"
          onClick={() => onDismiss(request.request_id)}
          aria-label={t("userRequest.dismiss")}
        >
          ×
        </button>
      </article>
    );
  });
}
