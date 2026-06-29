import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { API } from "../api";
import { eventBus } from "../lib/eventBus";

interface EditPair {
  old_string: string;
  new_string: string;
}

interface Change {
  uuid: string | null;
  tool: string | null;
  kind: "create" | "edit" | "patch";
  file_path: string | null;
  edits: EditPair[];
  why: string;
  ts: string | null;
  msg_id: string | null;
}

interface Turn {
  turn_index: number;
  user_prompt: string;
  ts: string | null;
  changes: Change[];
}

interface Props {
  sessionId: string;
}

const KIND_LABEL: Record<Change["kind"], string> = {
  create: "create",
  edit: "edit",
  patch: "patch",
};

/** Right-panel "Changes" view, grouped by turn. Each turn is a collapsible
 * card whose header is the user prompt that started it; expanding reveals the
 * file edits in that turn, each with its reasoning and a collapsible diff.
 * Backend-owned projection (GET /api/sessions/{id}/changes); refetched on the
 * `session_provenance_changed` WS ping. Pure render of backend state. */
export function ChangesPanel({ sessionId }: Props) {
  const { t } = useTranslation();
  const [turns, setTurns] = useState<Turn[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const r = await fetch(
        `${API}/api/sessions/${encodeURIComponent(sessionId)}/changes`,
        { credentials: "include" },
      );
      if (!r.ok) {
        const body = await r.text().catch(() => "");
        throw new Error(`HTTP ${r.status}: ${body || r.statusText}`);
      }
      const data = (await r.json()) as { turns: Turn[] };
      // Latest turn first — a changes feed reads top-down recent activity.
      setTurns([...(data.turns ?? [])].reverse());
    } catch (e) {
      setError((e as Error).message || "Failed to load changes");
    } finally {
      setLoading(false);
    }
  }, [sessionId]);

  useEffect(() => {
    void load();
    const refetchIf = (p: { session_id: string }) => {
      if (p.session_id === sessionId) void load();
    };
    const unsub = eventBus.subscribe("session_provenance_changed", refetchIf);
    return () => unsub();
  }, [sessionId, load]);

  const totalChanges = useMemo(
    () => (turns ?? []).reduce((n, t) => n + t.changes.length, 0),
    [turns],
  );

  if (loading && !turns) {
    return <div className="changes-panel-empty">{t("common.loading", "Loading…")}</div>;
  }
  if (error) {
    return <div className="changes-panel-error">{error}</div>;
  }
  if (turns && totalChanges === 0) {
    return (
      <div className="changes-panel-empty">{t("rightPanel.changesEmpty", "No file edits yet.")}</div>
    );
  }

  return (
    <div className="changes-panel-content">
      <div className="changes-panel-summary">
        {t("rightPanel.changesSummaryTurns", {
          changes: totalChanges,
          turns: turns?.length ?? 0,
          defaultValue: "{{changes}} changes · {{turns}} turns",
        })}
      </div>
      {turns?.map((turn, i) => (
        <TurnCard key={`${turn.turn_index}-${i}`} turn={turn} />
      ))}
    </div>
  );
}

function TurnCard({ turn }: { turn: Turn }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const isUngrouped = turn.turn_index < 0;
  const prompt = turn.user_prompt || (isUngrouped ? t("rightPanel.changesUngrouped", "Other edits") : "");
  const head = prompt.length > 140 ? prompt.slice(0, 140) + "…" : prompt;
  const label = isUngrouped
    ? t("rightPanel.changesUngrouped", "Other edits")
    : t("rightPanel.changesTurn", { n: turn.turn_index + 1, defaultValue: "Turn {{n}}" });
  return (
    <div className="changes-turn-card">
      <button
        type="button"
        className={`changes-turn-header ${open ? "open" : ""}`}
        onClick={() => setOpen((o) => !o)}
        title={prompt}
      >
        <span className={`changes-chevron ${open ? "open" : ""}`}>▸</span>
        <span className="changes-turn-label">{label}</span>
        <span className="changes-turn-count">{turn.changes.length}</span>
        <span className="changes-turn-prompt">{head || "—"}</span>
      </button>
      {open && (
        <div className="changes-turn-body">
          {turn.changes.map((c, i) => (
            <ChangeRow key={c.uuid ?? i} change={c} />
          ))}
        </div>
      )}
    </div>
  );
}

function ChangeRow({ change }: { change: Change }) {
  const { t } = useTranslation();
  const [diffOpen, setDiffOpen] = useState(false);
  const name = change.file_path ? change.file_path.split("/").pop() || change.file_path : "(no path)";
  const hasDiff = change.edits.some(
    (e) => change.kind !== "create" || !!e.new_string,
  ) && change.edits.some((e) => !!e.old_string || !!e.new_string);
  return (
    <div className="change-item">
      <div className="change-item-head">
        <span className={`change-kind-badge change-kind-${change.kind}`}>
          {KIND_LABEL[change.kind]}
        </span>
        <span className="change-file-name" title={change.file_path || undefined}>{name}</span>
        {hasDiff && (
          <button
            type="button"
            className="change-toggle"
            onClick={() => setDiffOpen((o) => !o)}
          >
            {diffOpen
              ? t("rightPanel.changesHide", "hide")
              : t("rightPanel.changesShow", "diff")}
          </button>
        )}
      </div>
      {change.why && <div className="change-why">{change.why}</div>}
      {diffOpen && hasDiff && (
        <div className="change-multi">
          {change.edits.map((e, i) => (
            <Diff key={i} oldString={e.old_string} newString={e.new_string} kind={change.kind} />
          ))}
        </div>
      )}
    </div>
  );
}

function Diff({
  oldString,
  newString,
  kind,
}: {
  oldString: string;
  newString: string;
  kind: Change["kind"];
}) {
  if (kind === "create") {
    return (
      <pre className="change-diff change-diff-new">
        <code>{newString}</code>
      </pre>
    );
  }
  return (
    <div className="change-diff-pair">
      {oldString ? (
        <pre className="change-diff change-diff-old">
          <code>{oldString}</code>
        </pre>
      ) : null}
      {newString ? (
        <pre className="change-diff change-diff-new">
          <code>{newString}</code>
        </pre>
      ) : null}
    </div>
  );
}
