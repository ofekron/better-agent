import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  fetchCommunications,
  postChatMessage,
  type CommunicationLogItem,
  type CommunicationLogResponse,
} from "../api";
import { runThreeStateSync } from "../progress/store";
import { linkifyFilePaths, sessionLinkMarker } from "../utils/linkifyFilePaths";
import Icon from "./Icon";

interface Props {
  sessionId?: string;
  senderSessionId?: string;
  mode: "page" | "panel";
  onBack?: () => void;
}

const KIND_LABEL: Record<string, string> = {
  mssg: "mssg",
  team_ask: "ask",
  delegate_task: "delegate",
  update: "update",
  chat: "chat",
};

function formatTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value || "";
  return date.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function shortBody(body: string): string {
  const oneLine = body.replace(/\s+/g, " ").trim();
  return oneLine.length > 140 ? `${oneLine.slice(0, 139)}…` : oneLine;
}

function SessionLink({ id, name }: { id?: string | null; name: string }) {
  if (!id) return <span>{name}</span>;
  return <>{linkifyFilePaths(sessionLinkMarker(id, name || id))}</>;
}

function participantNames(item: CommunicationLogItem, excludeId?: string | null): string {
  return (item.participants ?? [])
    .filter((participant) => participant.session_id && participant.session_id !== excludeId)
    .map((participant) => participant.name || participant.session_id)
    .join(", ");
}

function addressedTargetLabel(item: CommunicationLogItem): string {
  const target = item.addressed_target;
  if (!target?.value) return "";
  return target.pool_affinity_key
    ? `${target.value} · ${target.pool_affinity_key}`
    : target.value;
}

function chatItemsFrom(data: CommunicationLogResponse): CommunicationLogItem[] {
  return data.chats ?? (data.items ?? []).filter((item) => item.kind === "chat");
}

export function CommunicationsView({ sessionId, senderSessionId, mode, onBack }: Props) {
  const { t } = useTranslation();
  const [items, setItems] = useState<CommunicationLogItem[]>([]);
  const [chats, setChats] = useState<CommunicationLogItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await fetchCommunications(sessionId, mode === "page" ? 300 : 100);
      setItems(data.items ?? []);
      setChats(chatItemsFrom(data));
      setTotal(data.total ?? data.items?.length ?? 0);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [mode, sessionId]);

  useEffect(() => {
    let active = true;
    fetchCommunications(sessionId, mode === "page" ? 300 : 100)
      .then((data) => {
        if (!active) return;
        setItems(data.items ?? []);
        setChats(chatItemsFrom(data));
        setTotal(data.total ?? data.items?.length ?? 0);
      })
      .catch((e) => {
        if (!active) return;
        setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [mode, sessionId]);

  const title = mode === "page"
    ? t("communications.title")
    : t("rightPanel.communications");
  const directItems = useMemo(
    () => items.filter((item) => item.kind !== "chat"),
    [items],
  );
  const visibleCount = directItems.length + chats.length;
  const hasCommunications = visibleCount > 0;
  const subtitle = useMemo(() => {
    if (loading && visibleCount === 0) return t("common.loading");
    return t("communications.count", {
      count: visibleCount,
      total,
      defaultValue: "{{count}} shown · {{total}} total",
    });
  }, [loading, t, total, visibleCount]);

  return (
    <div className={mode === "page" ? "communications-page" : "communications-panel"}>
      <header className="communications-header">
        {mode === "page" && onBack && (
          <button className="an-btn" onClick={onBack}>
            <Icon name="chevron-left" size={15} />
            {t("common.back")}
          </button>
        )}
        <h1>
          <Icon name="chat" size={18} />
          {title}
        </h1>
        <span className="communications-subtitle">{subtitle}</span>
        <button className="an-btn an-btn-sm" onClick={load} disabled={loading}>
          {loading ? "…" : <Icon name="refresh" size={15} />}
        </button>
      </header>

      {error && <div className="communications-error">{error}</div>}

      {!loading && !hasCommunications ? (
        <div className="communications-empty">{t("communications.empty")}</div>
      ) : (
        <div className="communications-sections">
          {chats.length > 0 && (
            <section className="communications-section communications-section-chats">
              <div className="communications-section-header">
                <h2>{t("communications.chats")}</h2>
                <span>{chats.length}</span>
              </div>
              <div className="communications-chat-list">
                {chats.map((item) => (
                  <CommunicationChatCard
                    key={item.id}
                    item={item}
                    senderSessionId={senderSessionId ?? sessionId}
                    onPosted={load}
                  />
                ))}
              </div>
            </section>
          )}
          {directItems.length > 0 && (
            <section className="communications-section communications-section-direct">
              <div className="communications-section-header">
                <h2>{t("communications.directMessages")}</h2>
                <span>{directItems.length}</span>
              </div>
              <div className="communications-list">
                {directItems.map((item) => (
                  <CommunicationCard key={item.id} item={item} />
                ))}
              </div>
            </section>
          )}
        </div>
      )}
    </div>
  );
}

function CommunicationChatCard({
  item,
  senderSessionId,
  onPosted,
}: {
  item: CommunicationLogItem;
  senderSessionId?: string;
  onPosted: () => Promise<void>;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);
  const chatMessages = item.messages ?? [];
  const participantLabel = participantNames(item);
  const latestBody = chatMessages.length > 0
    ? chatMessages[chatMessages.length - 1].body
    : item.body;
  const canPost = Boolean(item.chat_id && senderSessionId);

  const submit = async () => {
    const message = draft.trim();
    if (!item.chat_id || !senderSessionId || !message || sending) return;
    setSending(true);
    setSendError(null);
    try {
      await runThreeStateSync({
        operationId: `communications:chat:${item.chat_id}`,
        action: t("communications.sendToChat"),
        reconcile: onPosted,
        mutate: () => postChatMessage(item.chat_id!, senderSessionId, message),
      });
      setDraft("");
      await onPosted();
    } catch (e) {
      setSendError(e instanceof Error ? e.message : String(e));
    } finally {
      setSending(false);
    }
  };

  return (
    <section className={`communication-chat-card communication-card-${item.kind}`}>
      <button
        type="button"
        className={`communication-chat-card-header ${open ? "open" : ""}`}
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
      >
        <span className="communication-chat-title">{item.chat_name || item.chat_id || item.to_name}</span>
        <span className="communication-chat-participants">{participantLabel}</span>
        <span className="communication-chat-preview">{shortBody(latestBody) || "—"}</span>
        <span className="communication-time">{formatTime(item.created_at)}</span>
        <Icon name="chevron-right" size={15} />
      </button>
      {open && (
        <div className="communication-body">
          <div className="communication-meta">
            <span>{item.status}</span>
            {item.chat_id && <span>{item.chat_id}</span>}
          </div>
          {chatMessages.length > 0 ? (
            <div className="communication-chat-messages">
              {chatMessages.map((message) => (
                <article className="communication-chat-message" key={message.id}>
                  <div className="communication-chat-message-meta">
                    <SessionLink id={message.from_session_id} name={message.from_name || message.from_session_id} />
                    <span>{formatTime(message.created_at)}</span>
                  </div>
                  <pre>{message.body}</pre>
                </article>
              ))}
            </div>
          ) : (
            <pre>{item.body}</pre>
          )}
          {canPost && (
            <form
              className="communication-chat-composer"
              onSubmit={(event) => {
                event.preventDefault();
                void submit();
              }}
            >
              <textarea
                className="communication-chat-input"
                value={draft}
                onChange={(event) => setDraft(event.target.value)}
                placeholder={t("communications.replyPlaceholder")}
                rows={2}
              />
              <button
                type="submit"
                className="communication-chat-send"
                disabled={sending || draft.trim().length === 0}
                aria-label={t("communications.sendToChat")}
                title={t("communications.sendToChat")}
              >
                {sending ? "…" : <Icon name="arrow-up" size={15} />}
              </button>
            </form>
          )}
          {sendError && <div className="communication-chat-error">{sendError}</div>}
        </div>
      )}
    </section>
  );
}

function CommunicationCard({ item }: { item: CommunicationLogItem }) {
  const [open, setOpen] = useState(false);
  const kind = KIND_LABEL[item.kind] ?? item.kind;
  const addressedTarget = addressedTargetLabel(item);
  const target = item.to_name || item.to_session_id || "";
  const participantLabel = participantNames(item);
  return (
    <section className={`communication-card communication-card-${item.kind}`}>
      <div className={`communication-card-header ${open ? "open" : ""}`}>
        <span className={`communication-kind communication-kind-${item.kind}`}>{kind}</span>
        <span className="communication-flow">
          <SessionLink id={item.from_session_id} name={item.from_name} />
          <span className="communication-arrow">→</span>
          <SessionLink
            id={item.to_session_id}
            name={target || "—"}
          />
        </span>
        <span className="communication-preview">{shortBody(item.body) || "—"}</span>
        <span className="communication-time">{formatTime(item.created_at)}</span>
        <button
          type="button"
          className={`communication-chevron ${open ? "open" : ""}`}
          onClick={() => setOpen((value) => !value)}
          aria-expanded={open}
          aria-label={`${kind} ${item.from_name} ${target || ""}`}
        >
          <Icon name="chevron-right" size={15} />
        </button>
      </div>
      {open && (
        <div className="communication-body">
          <div className="communication-meta">
            <span>{item.status}</span>
            {item.chat_id && <span>{item.chat_id}</span>}
            {addressedTarget && <span>{addressedTarget}</span>}
            {participantLabel && <span>{participantLabel}</span>}
          </div>
          <pre>{item.body}</pre>
        </div>
      )}
    </section>
  );
}
