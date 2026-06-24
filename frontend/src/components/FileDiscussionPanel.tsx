import { useMemo, useState } from "react";
import type { ChatMessage, FileDiscussion } from "../types";
import { MessageBubble } from "./MessageBubble";

interface Props {
  discussion: FileDiscussion;
  messages: ChatMessage[];
  pendingMessages: ChatMessage[];
  sessionId?: string;
  onSend?: (discussionId: string, prompt: string, clientId: string) => Promise<void>;
  onToggleCollapsed?: (discussionId: string, collapsed: boolean) => Promise<void>;
}

const EMPTY_THREAD_COLORS = new Map<string, string>();

export function FileDiscussionPanel({
  discussion,
  messages,
  pendingMessages,
  sessionId,
  onSend,
  onToggleCollapsed,
}: Props) {
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const collapsed = Boolean(discussion.collapsed);
  const threadMessages = useMemo(
    () => [...messages, ...pendingMessages].filter(
      (message) => message.file_discussion_id === discussion.id,
    ),
    [discussion.id, messages, pendingMessages],
  );

  const submit = async () => {
    const prompt = draft.trim();
    if (!prompt || !onSend || sending) return;
    const clientId = `file-discussion-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    setSending(true);
    setDraft("");
    try {
      await onSend(discussion.id, prompt, clientId);
    } finally {
      setSending(false);
    }
  };

  return (
    <div className={`file-discussion-panel ${collapsed ? "collapsed" : ""}`}>
      <div className="file-discussion-header">
        <button
          type="button"
          className="file-discussion-collapse"
          onClick={() => void onToggleCollapsed?.(discussion.id, !collapsed)}
          aria-label={collapsed ? "Expand discussion" : "Collapse discussion"}
        >
          {collapsed ? "\u25B6" : "\u25BC"}
        </button>
        <span className="file-discussion-title">
          {discussion.title || `Line ${discussion.line} discussion`}
        </span>
      </div>
      {!collapsed && (
        <div className="file-discussion-body">
          <div className="file-discussion-messages">
            {threadMessages.map((message) => (
              <MessageBubble
                key={message.id}
                message={message}
                sessionId={sessionId}
                threadColorMap={EMPTY_THREAD_COLORS}
              />
            ))}
          </div>
          <div className="file-discussion-input-row">
            <textarea
              className="file-discussion-input"
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={(event) => {
                if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
                  event.preventDefault();
                  void submit();
                }
              }}
            />
            <button
              type="button"
              className="btn-small"
              onClick={() => void submit()}
              disabled={!draft.trim() || sending}
            >
              Send
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
