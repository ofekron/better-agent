import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import Icon from "./Icon";
import type { PastedImage, Project, Session } from "../types";
import "../styles/sharePicker.css";

interface Props {
  /** Screenshot(s) handed in from the OS share sheet. */
  images: PastedImage[];
  projects: Project[];
  sessions: Session[];
  /** Attach the shared images to this session and navigate to it. */
  onPick: (sessionId: string) => void;
  onCancel: () => void;
}

const RECENT_COUNT = 5;

function recencyValue(value?: string): number {
  const parsed = Date.parse(value ?? "");
  return Number.isFinite(parsed) ? parsed : 0;
}

function byRecency(a: Session, b: Session): number {
  return recencyValue(b.updated_at) - recencyValue(a.updated_at);
}

/** Destination picker shown when the app is opened via the OS share
 *  sheet. Top: a one-tap row of the most recent sessions. Below: a
 *  PROJECT → SESSION drill-down. Selecting either attaches the shared
 *  image(s) to that session's composer and opens it. */
export function SharePicker({ images, projects, sessions, onPick, onCancel }: Props) {
  const { t } = useTranslation();
  const [openProjectPath, setOpenProjectPath] = useState<string | null>(null);

  const recent = useMemo(
    () => [...sessions].sort(byRecency).slice(0, RECENT_COUNT),
    [sessions]
  );

  const projectSessions = useMemo(() => {
    if (openProjectPath === null) return [];
    return sessions
      .filter((s) => s.cwd === openProjectPath)
      .sort(byRecency);
  }, [sessions, openProjectPath]);

  return (
    <div className="share-picker" data-testid="share-picker">
      <header className="share-picker-header">
        <h2>{t("share.title")}</h2>
        <button
          className="share-picker-cancel"
          onClick={onCancel}
          data-testid="share-cancel"
        >
          {t("app.cancel")}
        </button>
      </header>

      <div className="share-picker-thumbs" data-testid="share-thumbs">
        {images.map((img, i) => (
          <img key={i} src={img.dataUrl} alt={`shared ${i + 1}`} />
        ))}
      </div>

      {openProjectPath === null ? (
        <>
          {recent.length > 0 && (
            <section className="share-picker-section" data-testid="share-recent">
              <div className="share-picker-section-title">
                <Icon name="clock" size={14} /> {t("share.recent")}
              </div>
              {recent.map((s) => (
                <button
                  key={s.id}
                  className="share-picker-row"
                  data-testid="share-recent-session"
                  onClick={() => onPick(s.id)}
                >
                  <Icon name="chat" size={14} />
                  <span className="share-picker-row-label">{s.name}</span>
                </button>
              ))}
            </section>
          )}

          <section className="share-picker-section" data-testid="share-projects">
            <div className="share-picker-section-title">
              <Icon name="folder" size={14} /> {t("share.projects")}
            </div>
            {projects.length === 0 && (
              <div className="share-picker-empty">{t("share.noProjects")}</div>
            )}
            {projects.map((p) => (
              <button
                key={p.path}
                className="share-picker-row"
                data-testid="share-project"
                onClick={() => setOpenProjectPath(p.path)}
              >
                <Icon name="folder" size={14} />
                <span className="share-picker-row-label">{p.name}</span>
                <span className="share-picker-row-chevron">›</span>
              </button>
            ))}
          </section>
        </>
      ) : (
        <section className="share-picker-section" data-testid="share-project-sessions">
          <button
            className="share-picker-back"
            data-testid="share-back"
            onClick={() => setOpenProjectPath(null)}
          >
            ‹ {t("share.back")}
          </button>
          {projectSessions.length === 0 && (
            <div className="share-picker-empty">{t("share.noSessions")}</div>
          )}
          {projectSessions.map((s) => (
            <button
              key={s.id}
              className="share-picker-row"
              data-testid="share-project-session"
              onClick={() => onPick(s.id)}
            >
              <Icon name="chat" size={14} />
              <span className="share-picker-row-label">{s.name}</span>
            </button>
          ))}
        </section>
      )}
    </div>
  );
}
