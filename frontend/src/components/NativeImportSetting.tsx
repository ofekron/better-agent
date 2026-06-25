import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { API } from "../api";
import { trackPromise } from "../progress/store";

interface JobStatus {
  status: "idle" | "running" | "done" | "error";
  total: number;
  imported: number;
  skipped: number;
  failed: number;
  current: string;
  started_at: string;
  finished_at: string;
  provider_ids: string[];
  errors: { key: string; error: string }[];
}

interface PreviewSession {
  provider_id: string;
  provider_kind: string;
  native_id: string;
  title: string;
  cwd: string;
  created_at: string;
  already_imported: boolean;
}

interface Preview {
  sessions: PreviewSession[];
}

const EMPTY_STATUS: JobStatus = {
  status: "idle",
  total: 0,
  imported: 0,
  skipped: 0,
  failed: 0,
  current: "",
  started_at: "",
  finished_at: "",
  provider_ids: [],
  errors: [],
};

export function NativeImportSetting() {
  const { t } = useTranslation();
  const [status, setStatus] = useState<JobStatus>(EMPTY_STATUS);
  const [preview, setPreview] = useState<Preview>({ sessions: [] });
  const [error, setError] = useState("");
  const pollRef = useRef<number | null>(null);

  const running = status.status === "running";

  const fetchStatus = useCallback(async () => {
    try {
      const r = await fetch(`${API}/api/native-import/status`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setStatus(await r.json());
    } catch {
      /* ignore — best-effort polling */
    }
  }, []);

  const fetchPreview = useCallback(async () => {
    try {
      const { promise } = trackPromise("nativeImport:preview", () =>
        fetch(`${API}/api/native-import/sessions`),
      );
      const r = await promise;
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setPreview(await r.json());
    } catch (e) {
      setError(e instanceof Error ? e.message : "preview failed");
    }
  }, []);

  useEffect(() => {
    void fetchStatus();
    void fetchPreview();
  }, [fetchStatus, fetchPreview]);

  // Poll while a job is running; stop when it finishes, then refresh preview.
  useEffect(() => {
    if (!running) {
      if (pollRef.current) {
        window.clearInterval(pollRef.current);
        pollRef.current = null;
      }
      return;
    }
    pollRef.current = window.setInterval(() => {
      void fetchStatus().then(() => {
        setStatus((s) => {
          if (s.status !== "running") void fetchPreview();
          return s;
        });
      });
    }, 1000);
    return () => {
      if (pollRef.current) window.clearInterval(pollRef.current);
      pollRef.current = null;
    };
  }, [running, fetchStatus, fetchPreview]);

  const start = async () => {
    setError("");
    try {
      const { promise } = trackPromise("nativeImport:start", () =>
        fetch(`${API}/api/native-import`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ provider_ids: undefined }),
        }),
      );
      const r = await promise;
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setStatus(await r.json());
    } catch (e) {
      setError(e instanceof Error ? e.message : "start failed");
    }
  };

  const completed = status.imported + status.skipped + status.failed;
  const pct = status.total > 0 ? Math.min(100, Math.round((completed / status.total) * 100)) : 0;
  const pending = preview.sessions.filter((s) => !s.already_imported).length;

  return (
    <div className="native-import-setting">
      <div className="native-import-head">
        <div className="native-import-title">
          {t("settings.nativeImportTitle", "Import native sessions")}
        </div>
        <div className="native-import-desc">
          {t(
            "settings.nativeImportDesc",
            "Ingest every native CLI session (Claude, Codex) on disk into Better Agent. Runs in the background; imported sessions appear in your session list.",
          )}
        </div>
      </div>

      <div className="native-import-actions">
        <button
          className="native-import-btn"
          onClick={() => void start()}
          disabled={running || pending === 0}
          title={pending === 0 ? t("settings.nativeImportNothing", "Nothing new to import") : ""}
        >
          {running
            ? t("settings.nativeImportRunning", "Importing…")
            : t("settings.nativeImportBtn", "Import all native sessions")}
        </button>
        <span className="native-import-count">
          {pending > 0
            ? t("settings.nativeImportPending", {
                count: pending,
                defaultValue: "{{count}} session(s) ready",
              })
            : t("settings.nativeImportAllDone", "All sessions imported")}
        </span>
      </div>

      {running && (
        <div className="native-import-progress">
          <div className="native-import-progress-bar">
            <div className="native-import-progress-fill" style={{ width: `${pct}%` }} />
          </div>
          <div className="native-import-progress-meta">
            {status.imported} {t("settings.nativeImported", "imported")} ·{" "}
            {status.skipped} {t("settings.nativeSkipped", "skipped")} ·{" "}
            {status.failed} {t("settings.nativeFailed", "failed")} / {status.total}
          </div>
          {status.current && <div className="native-import-current">{status.current}</div>}
        </div>
      )}

      {status.status === "done" && status.finished_at && (
        <div className="native-import-result">
          {t("settings.nativeImportDone", {
            imported: status.imported,
            skipped: status.skipped,
            failed: status.failed,
            defaultValue:
              "Done — {{imported}} imported, {{skipped}} already present, {{failed}} failed.",
          })}
        </div>
      )}
      {status.status === "error" && (
        <div className="native-import-error">
          {t("settings.nativeImportErrored", "Import job crashed — see backend logs.")}
        </div>
      )}
      {status.errors.length > 0 && (
        <ul className="native-import-errors">
          {status.errors.slice(-5).map((e, i) => (
            <li key={`${e.key}-${i}`}>
              <code>{e.key}</code>: {e.error}
            </li>
          ))}
        </ul>
      )}
      {error && <div className="native-import-error">{error}</div>}
    </div>
  );
}
