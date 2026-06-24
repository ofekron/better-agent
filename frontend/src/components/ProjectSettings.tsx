import { useState, useEffect, useCallback } from "react";
import Icon, { type IconName } from "./Icon";
import { useTranslation } from "react-i18next";
import type { ProjectConfigFile } from "../types";
import { ProgressButton } from "../progress/ProgressButton";
import { trackedFetch, useOpProgress } from "../progress/store";
import { useBackButtonDismiss } from "../hooks/useBackButtonDismiss";

interface Props {
  cwd: string;
  onFileClick: (path: string) => void;
  onEngineerFile: (path: string, content: string) => void;
  onClose: () => void;
}

import { API } from "../api";

const CATEGORY_ORDER: Record<string, number> = {
  instructions: 0,
  settings: 1,
  skill: 2,
  hook: 3,
};

const CATEGORY_LABEL_KEYS: Record<string, string> = {
  instructions: "projectSettings.instructions",
  settings: "projectSettings.settings",
  skill: "projectSettings.skills",
  hook: "projectSettings.hooks",
};

const CATEGORY_ICONS: Record<string, IconName> = {
  instructions: "memo",
  settings: "settings",
  skill: "target",
  hook: "sliders",
};

export function ProjectSettings({ cwd, onFileClick, onEngineerFile, onClose }: Props) {
  const { t } = useTranslation();
  useBackButtonDismiss(true, onClose);
  const [files, setFiles] = useState<ProjectConfigFile[]>([]);
  const [error, setError] = useState<string | null>(null);
  const configOpId = `projectConfig:load:${cwd}`;
  const { inflight: loading } = useOpProgress(configOpId);

  useEffect(() => {
    let cancelled = false;
    setError(null);
    trackedFetch(configOpId, `${API}/api/project-config?cwd=${encodeURIComponent(cwd)}`)
      .then((r) => r.json())
      .then((data: { files: ProjectConfigFile[] }) => {
        if (!cancelled) {
          setFiles(data.files ?? []);
        }
      })
      .catch((e) => {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : t("projectSettings.failedToLoadConfig"));
        }
      });
    return () => { cancelled = true; };
  }, [cwd, configOpId, t]);

  const handleCreateFile = useCallback(
    async (filePath: string) => {
      try {
        await trackedFetch(`file:create:${filePath}`, `${API}/api/file`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: filePath, content: "" }),
        });
        // Refresh the list
        const refresh = await trackedFetch(
          configOpId,
          `${API}/api/project-config?cwd=${encodeURIComponent(cwd)}`,
        );
        const data = await refresh.json();
        setFiles(data.files ?? []);
        onFileClick(filePath);
      } catch (e) {
        alert(`${t("projectSettings.failedToCreateFile")}${e instanceof Error ? e.message : e}`);
      }
    },
    [cwd, onFileClick, configOpId, t]
  );

  const projectName = cwd.replace(/\/+$/, "").split("/").pop() || cwd;

  // Group files by category, sorted
  const grouped = new Map<string, ProjectConfigFile[]>();
  for (const f of files) {
    const list = grouped.get(f.category) ?? [];
    list.push(f);
    grouped.set(f.category, list);
  }
  const sortedCategories = [...grouped.keys()].sort(
    (a, b) => (CATEGORY_ORDER[a] ?? 99) - (CATEGORY_ORDER[b] ?? 99)
  );

  return (
    <div className="project-settings-overlay">
      <div className="project-settings-panel">
        <div className="project-settings-header">
          <div className="project-settings-title">
            <span className="project-settings-icon"><Icon name="settings" size={16} /></span>
            <span>{projectName}</span>
          </div>
          <button className="project-settings-close" onClick={onClose} aria-label="Close">
            &times;
          </button>
        </div>

        <div className="project-settings-subtitle">{cwd}</div>

        <div className="project-settings-content">
          {loading && <div className="project-settings-loading">{t("projectSettings.loading")}</div>}
          {error && <div className="project-settings-error">{error}</div>}
          {!loading && !error && sortedCategories.map((cat) => {
            const catFiles = grouped.get(cat) ?? [];
            return (
              <div key={cat} className="project-settings-group">
                <div className="project-settings-group-header">
                  <span>{CATEGORY_ICONS[cat] ? <Icon name={CATEGORY_ICONS[cat]} size={14} style={{ verticalAlign: "-2px" }} /> : null}</span>
                  <span>{CATEGORY_LABEL_KEYS[cat] ? t(CATEGORY_LABEL_KEYS[cat]) : cat}</span>
                  <span className="project-settings-group-count">{catFiles.length}</span>
                </div>
                {catFiles.map((f) => (
                  <div
                    key={f.path}
                    className={`project-settings-file ${f.exists ? "" : "missing"}`}
                  >
                    <div className="project-settings-file-info">
                      <div className="project-settings-file-name">{f.name}</div>
                      <div className="project-settings-file-desc">{f.description}</div>
                    </div>
                    <div className="project-settings-file-actions">
                      {f.exists ? (
                        <>
                          <button
                            className="btn-small"
                            onClick={() => onFileClick(f.path)}
                            title={t("projectSettings.viewEditTitle")}
                          >
                            Edit
                          </button>
                          <button
                            className="btn-small btn-accent"
                            onClick={() => onEngineerFile(f.path, "")}
                            title={t("projectSettings.aiEditTitle")}
                          >
                            <Icon name="settings" size={14} style={{ verticalAlign: "-2px" }} /> AI Edit
                          </button>
                        </>
                      ) : (
                        <ProgressButton
                          opId={[`file:create:${f.path}`, configOpId]}
                          className="btn-small"
                          onClick={() => handleCreateFile(f.path)}
                          loadingChildren={t("progress.creating")}
                          title={t("projectSettings.createTitle")}
                        >
                          Create
                        </ProgressButton>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
