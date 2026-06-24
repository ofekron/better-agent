import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";
import { useTranslation } from "react-i18next";
import { API } from "../api";
import { trackPromise } from "../progress/store";

interface PasswordManagerItem {
  service: string;
  account: string;
}

export function PasswordManagerSetting() {
  const { t } = useTranslation();
  const [service, setService] = useState("");
  const [account, setAccount] = useState("");
  const [password, setPassword] = useState("");
  const [items, setItems] = useState<PasswordManagerItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [deletingKey, setDeletingKey] = useState("");
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");

  const canSave = service.trim() && account.trim() && password && !saving;

  const loadItems = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const response = await trackPromise("passwordManager:list", () =>
        fetch(`${API}/api/extensions/ofek-dev.credential-broker/backend/settings/password-manager`, { credentials: "include" }),
      ).promise;
      if (!response.ok) throw new Error(await response.text());
      const data = (await response.json()) as { items?: PasswordManagerItem[] };
      setItems(Array.isArray(data.items) ? data.items : []);
    } catch (e) {
      setError(e instanceof Error ? e.message : t("settings.passwordManagerListFailed"));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    const timer = window.setTimeout(() => void loadItems(), 0);
    return () => window.clearTimeout(timer);
  }, [loadItems]);

  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (!canSave) return;
    setSaving(true);
    setStatus("");
    setError("");
    try {
      const response = await trackPromise("passwordManager:store", () =>
        fetch(`${API}/api/extensions/ofek-dev.credential-broker/backend/settings/password-manager/store`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "include",
          body: JSON.stringify({ service, account, password }),
        }),
      ).promise;
      if (!response.ok) throw new Error(await response.text());
      setPassword("");
      setStatus(t("settings.passwordManagerSaved"));
      await loadItems();
    } catch (e) {
      setError(e instanceof Error ? e.message : t("settings.passwordManagerFailed"));
    } finally {
      setSaving(false);
    }
  };

  const editItem = (item: PasswordManagerItem) => {
    setService(item.service);
    setAccount(item.account);
    setPassword("");
    setStatus(t("settings.passwordManagerEditing"));
    setError("");
  };

  const deleteItem = async (item: PasswordManagerItem) => {
    if (!window.confirm(t("settings.passwordManagerDeleteConfirm"))) return;
    const key = `${item.service}\n${item.account}`;
    setDeletingKey(key);
    setStatus("");
    setError("");
    try {
      const response = await trackPromise("passwordManager:delete", () =>
        fetch(`${API}/api/extensions/ofek-dev.credential-broker/backend/settings/password-manager`, {
          method: "DELETE",
          headers: { "Content-Type": "application/json" },
          credentials: "include",
          body: JSON.stringify(item),
        }),
      ).promise;
      if (!response.ok) throw new Error(await response.text());
      setStatus(t("settings.passwordManagerDeleted"));
      if (service === item.service && account === item.account) {
        setService("");
        setAccount("");
        setPassword("");
      }
      await loadItems();
    } catch (e) {
      setError(e instanceof Error ? e.message : t("settings.passwordManagerDeleteFailed"));
    } finally {
      setDeletingKey("");
    }
  };

  return (
    <form className="password-manager-setting" onSubmit={save}>
      <div className="password-manager-header">
        <div>
          <div className="password-manager-title">{t("settings.passwordManager")}</div>
          <div className="password-manager-hint">{t("settings.passwordManagerHint")}</div>
        </div>
        <button type="submit" className="btn-secondary" disabled={!canSave}>
          {saving ? t("settings.passwordManagerSaving") : t("settings.passwordManagerStore")}
        </button>
      </div>
      <div className="password-manager-grid">
        <label>
          <span>{t("settings.passwordManagerService")}</span>
          <input
            type="text"
            value={service}
            autoComplete="off"
            onChange={(e) => setService(e.target.value)}
          />
        </label>
        <label>
          <span>{t("settings.passwordManagerAccount")}</span>
          <input
            type="text"
            value={account}
            autoComplete="username"
            onChange={(e) => setAccount(e.target.value)}
          />
        </label>
        <label>
          <span>{t("settings.passwordManagerPassword")}</span>
          <input
            type="password"
            value={password}
            autoComplete="new-password"
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>
      </div>
      <div className="password-manager-list">
        {loading && <div className="password-manager-empty">{t("settings.passwordManagerLoading")}</div>}
        {!loading && items.length === 0 && (
          <div className="password-manager-empty">{t("settings.passwordManagerEmpty")}</div>
        )}
        {!loading && items.map((item) => {
          const key = `${item.service}\n${item.account}`;
          return (
            <div className="password-manager-item" key={key}>
              <div className="password-manager-item-main">
                <span className="password-manager-item-service">{item.service}</span>
                <span className="password-manager-item-account">{item.account}</span>
              </div>
              <div className="password-manager-item-actions">
                <button type="button" className="btn-secondary" onClick={() => editItem(item)}>
                  {t("settings.passwordManagerEdit")}
                </button>
                <button
                  type="button"
                  className="btn-danger"
                  disabled={deletingKey === key}
                  onClick={() => void deleteItem(item)}
                >
                  {deletingKey === key
                    ? t("settings.passwordManagerDeleting")
                    : t("settings.passwordManagerDelete")}
                </button>
              </div>
            </div>
          );
        })}
      </div>
      {status && <div className="password-manager-status">{status}</div>}
      {error && <div className="setup-error">{error}</div>}
    </form>
  );
}
