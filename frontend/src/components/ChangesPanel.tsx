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

interface Props {
  sessionId: string;
}

const KIND_LABEL: Record<Change["kind"], string> = {
  create: "create",
  edit: "edit",
  patch: "patch",
};

/** Right-panel "Changes" view: every file edit made in the session plus the
 * reasoning that preceded each one. Backend-owned projection
 * (GET /api/sessions/{id}/changes); refetched on the
 * `session_provenance_changed` WS ping. Pure render of backend state. */
export function ChangesPanel({ sessionId }: Props) {
  const { t } = useTranslation();
  const [changes, setChanges] = useState<Change[] | null>(null);
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
      const data = (await r.json()) as { changes: Change[] };
      setChanges(data.changes ?? []);
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

  // Group changes by file path (chronological within each file). Edits
  // without a path (raw apply_patch with no extracted path) bucket together.
  const groups = useMemo(() => {
    const map = new Map<string, Change[]>();
    const order: string[] = [];
    for (const c of changes ?? []) {
      const key = c.file_path ?? "";
      if (!map.has(key)) {
        map.set(key, []);
        order.push(key);
      }
      map.get(key)!.push(c);
    }
    return order.map((k) => ({ path: k, items: map.get(k)! }));
  }, [changes]);

  if (loading && !changes) {
    return <div className="changes-panel-empty">{t("common.loading", "Loading…")}</div>;
  }
  if (error) {
    return <div className="changes-panel-error">{error}</div>;
  }
  if (changes && changes.length === 0) {
    return (
      <div className="changes-panel-empty">{t("rightPanel.changesEmpty", "No file edits yet.")}</div>
    );
  }

  return (
    <div className="changes-panel-content">
      <div className="changes-panel-summary">
        {t("rightPanel.changesSummary", {
          changes: changes?.length ?? 0,
          files: groups.length,
          defaultValue: "{{changes}} changes · {{files}} files",
        })}
      </div>
      {groups.map((g, gi) => (
        <FileGroup key={gi} path={g.path} items={g.items} />
      ))}
    </div>
  );
}

function FileGroup({ path, items }: { path: string; items: Change[] }) {
  const name = path ? path.split("/").pop() || path : "(no path)";
  return (
    <div className="changes-file-card">
      <div className="changes-file-header" title={path || undefined}>
        <span className="changes-file-name">{name}</span>
        {path && <span className="changes-file-count">{items.length}</span>}
      </div>
      <div className="changes-file-body">
        {items.map((c, i) => (
          <ChangeItem key={c.uuid ?? i} change={c} />
        ))}
      </div>
    </div>
  );
}

function ChangeItem({ change }: { change: Change }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const pair = change.edits[0];
  const hasDiff =
    !!pair &&
    (change.kind !== "create" || !!pair.new_string) &&
    !!(pair.old_string || pair.new_string);
  return (
    <div className="change-item">
      <div className="change-item-head">
        <span className={`change-kind-badge change-kind-${change.kind}`}>
          {KIND_LABEL[change.kind]}
        </span>
        {change.ts && <span className="change-ts">{change.ts}</span>}
        {hasDiff && (
          <button
            type="button"
            className="change-toggle"
            onClick={() => setOpen((o) => !o)}
          >
            {open
              ? t("rightPanel.changesHide", "hide")
              : t("rightPanel.changesShow", "diff")}
          </button>
        )}
      </div>
      {change.why && <div className="change-why">{change.why}</div>}
      {open && hasDiff && pair && <Diff oldString={pair.old_string} newString={pair.new_string} kind={change.kind} />}
      {open && change.edits.length > 1 && (
        <div className="change-multi">
          {change.edits.slice(1).map((e, i) => (
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
