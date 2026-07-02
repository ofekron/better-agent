import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  fetchCommunications,
  type CommunicationLogItem,
} from "../api";
import { linkifyFilePaths, sessionLinkMarker } from "../utils/linkifyFilePaths";
import Icon from "./Icon";

interface Props {
  sessionId?: string;
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

export function CommunicationsView({ sessionId, mode, onBack }: Props) {
  const { t } = useTranslation();
  const [items, setItems] = useState<CommunicationLogItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await fetchCommunications(sessionId, mode === "page" ? 300 : 100);
      setItems(data.items ?? []);
      setTotal(data.total ?? data.items?.length ?? 0);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [mode, sessionId]);

  useEffect(() => {
    void load();
  }, [load]);

  const title = mode === "page"
    ? t("communications.title")
    : t("rightPanel.communications");
  const subtitle = useMemo(() => {
    if (loading && items.length === 0) return t("common.loading");
    return t("communications.count", {
      count: items.length,
      total,
      defaultValue: "{{count}} shown · {{total}} total",
    });
  }, [items.length, loading, t, total]);

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

      {!loading && items.length === 0 ? (
        <div className="communications-empty">{t("communications.empty")}</div>
      ) : (
        <div className="communications-list">
          {items.map((item) => (
            <CommunicationCard key={item.id} item={item} />
          ))}
        </div>
      )}
    </div>
  );
}

function CommunicationCard({ item }: { item: CommunicationLogItem }) {
  const [open, setOpen] = useState(false);
  const kind = KIND_LABEL[item.kind] ?? item.kind;
  const target = item.kind === "chat"
    ? item.chat_name || item.chat_id || item.to_name
    : item.to_name;
  return (
    <section className={`communication-card communication-card-${item.kind}`}>
      <div className={`communication-card-header ${open ? "open" : ""}`}>
        <span className={`communication-kind communication-kind-${item.kind}`}>{kind}</span>
        <span className="communication-flow">
          <SessionLink id={item.from_session_id} name={item.from_name} />
          <span className="communication-arrow">→</span>
          <SessionLink
            id={item.kind === "chat" ? undefined : item.to_session_id}
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
          </div>
          <pre>{item.body}</pre>
        </div>
      )}
    </section>
  );
}
