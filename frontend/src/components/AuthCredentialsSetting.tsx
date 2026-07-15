import { useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import { API } from "../api";
import { setStoredToken } from "../bearerAuth";
import { runThreeStateSync } from "../progress/store";

export function AuthCredentialsSetting() {
  const { t } = useTranslation();
  const [currentUsername, setCurrentUsername] = useState("");
  const [currentPassword, setCurrentPassword] = useState("");
  const [newUsername, setNewUsername] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setSaving(true);
    setError("");
    setSaved(false);
    const nextUsername = newUsername.trim();
    try {
      const { result: response } = await runThreeStateSync({
        operationId: "authCredentials:save",
        action: t("settings.authCredentialsSave"),
        info: nextUsername,
        reconcile: () => undefined,
        mutate: async () => {
          const response = await fetch(`${API}/api/auth/change_credentials`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            credentials: "include",
            body: JSON.stringify({
              current_username: currentUsername.trim(),
              current_password: currentPassword,
              new_username: nextUsername,
              new_password: newPassword,
            }),
          });
          if (!response.ok) {
            throw new Error(response.status === 401
              ? t("settings.authCredentialsInvalid")
              : t("settings.authCredentialsFailed"));
          }
          return response;
        },
      });
      const body = await response.json() as { username?: unknown; token?: unknown };
      if (typeof body.token === "string") setStoredToken(body.token);
      if (typeof body.username === "string") {
        window.dispatchEvent(new CustomEvent("auth_user_changed", { detail: { username: body.username } }));
      }
      setCurrentUsername("");
      setCurrentPassword("");
      setNewUsername("");
      setNewPassword("");
      setSaved(true);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : t("settings.authCredentialsNetworkFailed"));
    } finally {
      setSaving(false);
    }
  };

  return (
    <form className="auth-credentials-setting" onSubmit={(event) => void submit(event)}>
      <div className="auth-credentials-grid">
        <label className="auth-credentials-field">
          <span>{t("settings.authCredentialsCurrentUsername")}</span>
          <input
            type="text"
            autoComplete="username"
            value={currentUsername}
            disabled={saving}
            onChange={(event) => setCurrentUsername(event.target.value)}
            required
          />
        </label>
        <label className="auth-credentials-field">
          <span>{t("settings.authCredentialsCurrentPassword")}</span>
          <input
            type="password"
            autoComplete="current-password"
            value={currentPassword}
            disabled={saving}
            onChange={(event) => setCurrentPassword(event.target.value)}
            required
          />
        </label>
        <label className="auth-credentials-field">
          <span>{t("settings.authCredentialsNewUsername")}</span>
          <input
            type="text"
            autoComplete="username"
            value={newUsername}
            disabled={saving}
            onChange={(event) => setNewUsername(event.target.value)}
            required
          />
        </label>
        <label className="auth-credentials-field">
          <span>{t("settings.authCredentialsNewPassword")}</span>
          <input
            type="password"
            autoComplete="new-password"
            value={newPassword}
            disabled={saving}
            onChange={(event) => setNewPassword(event.target.value)}
            required
          />
        </label>
      </div>
      <div className="auth-credentials-actions">
        <button type="submit" className="setup-save-btn" disabled={saving}>
          {saving ? t("settings.authCredentialsSaving") : t("settings.authCredentialsSave")}
        </button>
        {saved && <span className="auth-credentials-status">{t("settings.authCredentialsSaved")}</span>}
        {error && <span className="auth-credentials-error">{error}</span>}
      </div>
    </form>
  );
}
