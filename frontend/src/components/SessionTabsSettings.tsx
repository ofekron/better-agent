import { useState, useEffect } from "react";
import { useTranslation } from "react-i18next";
import { Select } from "./Select";
import { API } from "../api";
import { trackPromise } from "../progress/store";
import { eventBus } from "../lib/eventBus";

const SORT_VALUES = ["updated_at", "last_user_prompt_at", "last_opened_at"] as const;
type SortValue = (typeof SORT_VALUES)[number];

function normalize(value: unknown): SortValue {
  return SORT_VALUES.includes(value as SortValue) ? (value as SortValue) : "last_opened_at";
}

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
    eventBus.publish("user_prefs_changed", body);
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
        <Select<SortValue>
          value={sort}
          disabled={saving || !visible}
          onChange={(v) => {
            const next = normalize(v);
            void patch({ sessions_tabs_sort: next }, () => setSort(next));
          }}
          options={[
            { value: "last_opened_at", label: t("session.sortByOpened") },
            { value: "updated_at", label: t("session.sortByModified") },
            { value: "last_user_prompt_at", label: t("session.sortByUserPrompt") },
          ]}
        />
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
