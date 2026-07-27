import { useState } from "react";
import { useTranslation } from "react-i18next";
import { API } from "src/api";
import { trackPromise } from "src/progress/store";
import type {
  ExtensionAppSettingItem,
  ExtensionAppSettingsSection as SectionDescriptor,
} from "src/hooks/useExtensionAppSettings";

/** Renders one extension-contributed app Settings section from its backend
 * descriptor. Every control writes straight through to the owning extension's
 * settings endpoint; the section refetches from the backend afterwards, so
 * nothing here holds state the backend does not confirm. */
export function ExtensionAppSettingsSection({
  section,
  onSaved,
}: {
  section: SectionDescriptor;
  onSaved: () => Promise<void>;
}) {
  const { t } = useTranslation();
  return (
    <div className="extension-app-settings">
      {section.description && (
        <div className="settings-hint">{section.description}</div>
      )}
      {section.items.map((item) => (
        <SettingControl
          key={`${item.extension_id}:${item.key}`}
          item={item}
          onSaved={onSaved}
          t={t}
        />
      ))}
    </div>
  );
}

function SettingControl({
  item,
  onSaved,
  t,
}: {
  item: ExtensionAppSettingItem;
  onSaved: () => Promise<void>;
  t: (key: string, options?: Record<string, unknown>) => string;
}) {
  const [saving, setSaving] = useState(false);
  const [failed, setFailed] = useState(false);

  const save = async (value: unknown) => {
    setSaving(true);
    setFailed(false);
    try {
      const res = await trackPromise(
        `extensionAppSettings:save:${item.extension_id}:${item.key}`,
        () =>
          fetch(`${API}/api/extensions/${encodeURIComponent(item.extension_id)}/settings`, {
            method: "PATCH",
            credentials: "include",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ key: item.key, value }),
          }),
      ).promise;
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      await onSaved();
    } catch {
      setFailed(true);
    } finally {
      setSaving(false);
    }
  };

  const status = saving ? (
    <span className="extension-app-settings-status">{t("settings.extensionAppSettingsSaving")}</span>
  ) : failed ? (
    <span className="extension-app-settings-status is-error">
      {t("settings.extensionAppSettingsFailed")}
    </span>
  ) : null;

  const owner = (
    <div className="extension-app-settings-owner">
      {t("settings.extensionAppSettingsOwner", { name: item.extension_name })}
    </div>
  );

  if (item.type === "boolean") {
    return (
      <div className="extension-app-settings-row">
        <label className="extension-app-settings-toggle">
          <input
            type="checkbox"
            checked={item.value === true}
            disabled={saving}
            onChange={(e) => void save(e.target.checked)}
          />
          <span>{item.label}</span>
        </label>
        {item.help && <div className="settings-hint">{item.help}</div>}
        {owner}
        {status}
      </div>
    );
  }

  if (item.enum.length) {
    return (
      <div className="extension-app-settings-row">
        <label className="extension-app-settings-field">
          <span>{item.label}</span>
          <select
            value={String(item.value ?? "")}
            disabled={saving}
            onChange={(e) =>
              void save(item.type === "number" ? Number(e.target.value) : e.target.value)
            }
          >
            {item.enum.map((option) => (
              <option key={String(option)} value={String(option)}>
                {String(option)}
              </option>
            ))}
          </select>
        </label>
        {item.help && <div className="settings-hint">{item.help}</div>}
        {owner}
        {status}
      </div>
    );
  }

  return (
    <div className="extension-app-settings-row">
      <label className="extension-app-settings-field">
        <span>{item.label}</span>
        <input
          type={item.type === "number" ? "number" : "text"}
          defaultValue={String(item.value ?? "")}
          disabled={saving}
          onBlur={(e) => {
            const next = item.type === "number" ? Number(e.target.value) : e.target.value;
            if (next === item.value) return;
            void save(next);
          }}
        />
      </label>
      {item.help && <div className="settings-hint">{item.help}</div>}
      {owner}
      {status}
    </div>
  );
}
