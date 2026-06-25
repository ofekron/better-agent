import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { API } from "../api";
import { advSyncForksFor } from "../hooks/useSession";
import type { AdvSyncOverlay, ChatMessage, Session } from "../types";
import { TurnGroup } from "./MessageBubble";

interface Props {
  overlayId: string;
  parentId: string;
}

/** Dedicated full-window view that renders the two adversarial-sync
 *  forks side by side. Opened via `window.open(?adv_sync_overlay=…)`
 *  from the main app when the user clicks a converged overlay span.
 *
 *  This view intentionally does NOT mount the main App's sidebar /
 *  projects / sessions chrome — it's a focused drill-down on a single
 *  overlay. State is loaded fresh via REST and refreshed by polling
 *  every 2s (no WS subscription wiring needed); adv-sync runs are
 *  short-lived (≤6 rounds) so the poll cost is negligible.
 */
export function AdvSyncWindow({ overlayId, parentId }: Props) {
  const { t } = useTranslation();
  const [tree, setTree] = useState<Session | null>(null);
  const [error, setError] = useState<string | null>(null);
  const intervalRef = useRef<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const res = await fetch(`${API}/api/sessions/${parentId}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const t = (await res.json()) as Session;
        if (!cancelled) setTree(t);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      }
    };
    load();
    intervalRef.current = window.setInterval(load, 2000);
    return () => {
      cancelled = true;
      if (intervalRef.current != null) window.clearInterval(intervalRef.current);
    };
  }, [parentId]);

  const overlay: AdvSyncOverlay | undefined = useMemo(
    () =>
      (tree?.adv_sync_overlays ?? []).find((o) => o.id === overlayId),
    [tree, overlayId],
  );
  const forks = useMemo(
    () => (tree && overlay ? advSyncForksFor(tree, overlay) : []),
    [tree, overlay],
  );

  // Stop polling once the run reaches a terminal state — saves cycles
  // on a window the user may leave open indefinitely.
  useEffect(() => {
    if (
      overlay &&
      overlay.status !== "running" &&
      intervalRef.current != null
    ) {
      window.clearInterval(intervalRef.current);
      intervalRef.current = null;
    }
  }, [overlay?.status]);

  useEffect(() => {
    const previousTitle = document.title;
    document.title = `Adv-sync · ${overlay?.original_text?.slice(0, 60) ?? ""}`;
    return () => {
      document.title = previousTitle;
    };
  }, [overlay?.original_text]);

  if (error) {
    return (
      <div className="adv-sync-window adv-sync-window-error">
        <h2>{t("advSync.title")}</h2>
        <p>{t("advSync.failedToLoad", { error })}</p>
      </div>
    );
  }
  if (!tree || !overlay) {
    return (
      <div className="adv-sync-window adv-sync-window-loading">
        <h2>{t("advSync.title")}</h2>
        <p>{t("progress.loading")}</p>
      </div>
    );
  }
  if (forks.length !== 2) {
    return (
      <div className="adv-sync-window adv-sync-window-error">
        <h2>{t("advSync.title")}</h2>
        <p>{t("advSync.missingForks")}</p>
      </div>
    );
  }
  const [supportive, adversarial] = forks;

  return (
    <div className="adv-sync-window">
      <header className="adv-sync-window-header">
        <div className="adv-sync-window-titlerow">
          <h2>{t("advSync.title")}</h2>
          <span className={`adv-sync-window-status adv-sync-${overlay.status}`}>
            {t(`advSync.status.${overlay.status}`, { defaultValue: overlay.status })}
            {overlay.status === "running"
              ? ` · ${t("advSync.round", {
                  completed: overlay.rounds_completed,
                  max: overlay.max_rounds,
                })}`
              : ""}
          </span>
        </div>
        <div className="adv-sync-window-textcompare">
          <div className="adv-sync-window-text-block">
            <div className="adv-sync-window-text-label">{t("advSync.original")}</div>
            <pre className="adv-sync-window-text">{overlay.original_text}</pre>
          </div>
          {overlay.agreed_text != null && (
            <div className="adv-sync-window-text-block">
              <div className="adv-sync-window-text-label">{t("advSync.agreed")}</div>
              <pre className="adv-sync-window-text adv-sync-window-text-agreed">
                {overlay.agreed_text}
              </pre>
            </div>
          )}
        </div>
        {overlay.error && (
          <div className="adv-sync-window-error-banner">
            {t("advSync.error", { error: overlay.error })}
          </div>
        )}
      </header>
      <div className="adv-sync-window-grid">
        <AdvSyncPane
          label={`⊕ ${t("advSync.supportive")}`}
          session={supportive}
          parentTree={tree}
        />
        <AdvSyncPane
          label={`⊖ ${t("advSync.adversarial")}`}
          session={adversarial}
          parentTree={tree}
        />
      </div>
    </div>
  );
}

interface PaneProps {
  label: string;
  session: Session;
  parentTree: Session;
}

/** Renders one fork's post-fork-point messages as user/assistant
 *  turn groups via the shared `TurnGroup` so tool calls, code blocks,
 *  thinking, etc. all render identically to the main chat view. */
function AdvSyncPane({ label, session, parentTree }: PaneProps) {
  const fp = session.fork_point_seq;
  const postForkMessages = useMemo(() => {
    const msgs = session.messages ?? [];
    if (typeof fp !== "number") return msgs;
    return msgs.filter((m) => typeof m.seq === "number" && m.seq > fp);
  }, [session.messages, fp]);

  const groups = useMemo(() => {
    const out: { initiator: ChatMessage; response?: ChatMessage }[] = [];
    for (let i = 0; i < postForkMessages.length; i++) {
      const m = postForkMessages[i];
      if (m.role === "user") {
        const next = postForkMessages[i + 1];
        if (next && next.role === "assistant") {
          out.push({ initiator: m, response: next });
          i++;
        } else {
          out.push({ initiator: m });
        }
      }
    }
    return out;
  }, [postForkMessages]);

  return (
    <div className="adv-sync-window-pane">
      <div className="adv-sync-window-pane-header">{label}</div>
      <div className="adv-sync-window-pane-body">
        {groups.length === 0 ? (
          <div className="adv-sync-window-pane-empty">
            No turns yet — waiting for the driver…
          </div>
        ) : (
          groups.map((g) => (
            <TurnGroup
              key={g.initiator.id}
              initiatorMessage={g.initiator}
              responseMessage={g.response}
              sessionId={session.id}
              threadColorMap={undefined}
              defaultCollapsed={!!g.response}
              orchestrationMode={parentTree.orchestration_mode}
              runs={[]}
            />
          ))
        )}
      </div>
    </div>
  );
}
