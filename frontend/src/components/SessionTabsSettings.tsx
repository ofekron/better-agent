import { useState, useEffect } from "react";
import { useTranslation } from "react-i18next";
import { Select } from "./Select";
import { API } from "../api";
import { trackPromise } from "../progress/store";
import { eventBus } from "../lib/eventBus";
import { queueWrite } from "../utils/writeBacklog";

const SORT_VALUES = ["updated_at", "last_user_prompt_at", "last_opened_at"] as const;
type SortValue = (typeof SORT_VALUES)[number];

function normalize(value: unknown): SortValue {
  return SORT_VALUES.includes(value as SortValue) ? (value as SortValue) : "last_opened_at";
}

export function SessionTabsSettings() {
  const { t } = useTranslation();
  const [sort, setSort] = useState<SortValue>("last_opened_at");
  const [visible, setVisible] = useState(true);

  useEffect(() => {
    trackPromise("sessionTabs:load", () => fetch(`${API}/api/user-prefs`))
      .promise
      .then((r: Response) => r.json())
      .then((data: {
        sessions_tabs_sort?: unknown;
        sessions_tabs_visible?: unknown;
      }) => {
        setSort(normalize(data.sessions_tabs_sort));
        if (typeof data.sessions_tabs_visible === "boolean") setVisible(data.sessions_tabs_visible);
      })
      .catch(() => {});
  }, []);

  // Optimistic write-through: apply locally + fan out to same-tab listeners
  // immediately, then queue the backend write (durable; drains on reconnect).
  // The backend's `user_prefs_changed` broadcast converges every tab once it
  // acknowledges — including this one.
  const patch = (body: Record<string, unknown>, apply: () => void) => {
    apply();
    eventBus.publish("user_prefs_changed", body);
    const field = Object.keys(body)[0] ?? "misc";
    queueWrite({
      method: "PATCH",
      url: "/api/user-prefs",
      body,
      key: `user_prefs:${field}`,
    });
  };

  return (
    <div className="context-strategy-setting">
      <label className="context-strategy-row">
        <span>{t("settings.sessionTabsVisible")}</span>
        <input
          type="checkbox"
          checked={visible}
          onChange={(e) =>
            patch(
              { sessions_tabs_visible: e.target.checked },
              () => setVisible(e.target.checked),
            )
          }
        />
      </label>
      <label className="context-strategy-row">
        <span>{t("settings.sessionTabsSort")}</span>
        <Select<SortValue>
          value={sort}
          disabled={!visible}
          onChange={(v) => {
            const next = normalize(v);
            patch({ sessions_tabs_sort: next }, () => setSort(next));
          }}
          options={[
            { value: "last_opened_at", label: t("session.sortByOpened") },
            { value: "updated_at", label: t("session.sortByModified") },
            { value: "last_user_prompt_at", label: t("session.sortByUserPrompt") },
          ]}
        />
      </label>
    </div>
  );
}
