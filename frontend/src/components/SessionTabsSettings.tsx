import { useState, useEffect } from "react";
import { useTranslation } from "react-i18next";
import { API } from "../api";
import { trackPromise } from "../progress/store";

const SORT_VALUES = ["updated_at", "last_user_prompt_at", "last_opened_at"] as const;
type SortValue = (typeof SORT_VALUES)[number];

function normalize(value: unknown): SortValue {
  return SORT_VALUES.includes(value as SortValue) ? (value as SortValue) : "last_opened_at";
}

/** Settings for the open-session tabs bar (`SessionTabs`): whether it is
 * shown, and how its tabs are ordered. Backed by the `sessions_tabs_visible`
 * and `sessions_tabs_sort` user prefs. Distinct from the sidebar session
 * list, which has its own `session_sort`. */
export function SessionTabsSettings() {
  const { t } = useTranslation();
  const [sort, setSort] = useState<SortValue>("last_opened_at");
  const [statusSort, setStatusSort] = useState(false);
  const [visible, setVisible] = useState(true);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    trackPromise("sessionTabs:load", () => fetch(`${API}/api/user-prefs`))
      .promise
      .then((r: Response) => r.json())
      .then((data: {
        sessions_tabs_sort?: unknown;
        sessions_tabs_status_sort?: unknown;
        sessions_tabs_visible?: unknown;
      }) => {
        setSort(normalize(data.sessions_tabs_sort));
        if (typeof data.sessions_tabs_status_sort === "boolean") setStatusSort(data.sessions_tabs_status_sort);
        if (typeof data.sessions_tabs_visible === "boolean") setVisible(data.sessions_tabs_visible);
      })
      .catch(() => {});
  }, []);

  const patch = async (body: Record<string, unknown>, apply: () => void) => {
    setSaving(true);
    try {
      await trackPromise(
        "sessionTabs:save",
        () => fetch(`${API}/api/user-prefs`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }),
      ).promise;
    } catch {
      return;
    } finally {
      setSaving(false);
    }
    apply();
  };

  return (
    <div className="context-strategy-setting">
      <label className="context-strategy-row">
        <span>{t("settings.sessionTabsVisible")}</span>
        <input
          type="checkbox"
          checked={visible}
          disabled={saving}
          onChange={(e) => void patch(
            { sessions_tabs_visible: e.target.checked },
            () => setVisible(e.target.checked),
          )}
        />
      </label>
      <label className="context-strategy-row">
        <span>{t("settings.sessionTabsSort")}</span>
        <select
          value={sort}
          disabled={saving || !visible}
          onChange={(e) => {
            const next = normalize(e.target.value);
            void patch({ sessions_tabs_sort: next }, () => setSort(next));
          }}
        >
          <option value="last_opened_at">{t("session.sortByOpened")}</option>
          <option value="updated_at">{t("session.sortByModified")}</option>
          <option value="last_user_prompt_at">{t("session.sortByUserPrompt")}</option>
        </select>
      </label>
      <label className="context-strategy-row" title={t("session.groupByStatusHint")}>
        <span>{t("session.groupByStatus")}</span>
        <input
          type="checkbox"
          checked={statusSort}
          disabled={saving || !visible}
          onChange={(e) => void patch(
            { sessions_tabs_status_sort: e.target.checked },
            () => setStatusSort(e.target.checked),
          )}
        />
      </label>
    </div>
  );
}
