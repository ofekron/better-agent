import { motion } from "framer-motion";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  getMobileNotificationPreferences,
  updateMobileNotificationPreferences,
  type MobileNotificationPreferences,
} from "../api";
import { trackPromise } from "../progress/store";
import { getOrCreateMobilePushDeviceId } from "../utils/mobilePushNotifications";

type PreferenceKey = keyof MobileNotificationPreferences;

const OPTIONS: Array<{ key: PreferenceKey; label: string; hint: string }> = [
  {
    key: "pending_approvals",
    label: "settings.mobileNotificationsApprovals",
    hint: "settings.mobileNotificationsApprovalsHint",
  },
  {
    key: "pending_questions",
    label: "settings.mobileNotificationsQuestions",
    hint: "settings.mobileNotificationsQuestionsHint",
  },
  {
    key: "completed_turns",
    label: "settings.mobileNotificationsCompleted",
    hint: "settings.mobileNotificationsCompletedHint",
  },
];

export function MobileNotificationSettings() {
  const { t } = useTranslation();
  const [preferences, setPreferences] = useState<MobileNotificationPreferences | null>(null);
  const [saving, setSaving] = useState<PreferenceKey | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    trackPromise("mobileNotifications:load", async () => {
      const deviceId = await getOrCreateMobilePushDeviceId();
      return getMobileNotificationPreferences(deviceId);
    }).promise.then((value) => {
      if (!cancelled) setPreferences(value);
    }).catch(() => {
      if (!cancelled) setError(t("settings.mobileNotificationsUnavailable"));
    });
    return () => {
      cancelled = true;
    };
  }, [t]);

  const update = async (key: PreferenceKey, enabled: boolean) => {
    setSaving(key);
    setError("");
    try {
      const deviceId = await getOrCreateMobilePushDeviceId();
      const next = await trackPromise("mobileNotifications:save", () =>
        updateMobileNotificationPreferences(deviceId, { [key]: enabled }),
      ).promise;
      setPreferences(next);
    } catch {
      setError(t("settings.mobileNotificationsSaveFailed"));
    } finally {
      setSaving(null);
    }
  };

  return (
    <section className="mobile-notification-settings">
      <h3>{t("settings.mobileNotificationsTitle")}</h3>
      <p className="settings-hint">{t("settings.mobileNotificationsHint")}</p>
      {!preferences && !error && <p>{t("settings.mobileNotificationsLoading")}</p>}
      {preferences && OPTIONS.map((option) => (
        <label className="extension-ui-settings-toggle" key={option.key}>
          <input
            type="checkbox"
            checked={preferences[option.key]}
            disabled={saving !== null}
            onChange={(event) => void update(option.key, event.target.checked)}
          />
          <span>
            <strong>{t(option.label)}</strong>
            <small>{t(option.hint)}</small>
          </span>
        </label>
      ))}
      <motion.p
        aria-live="polite"
        initial={false}
        animate={{ opacity: error || saving ? 1 : 0, y: error || saving ? 0 : -4 }}
        className={error ? "settings-error" : "settings-hint"}
      >
        {error || (saving ? t("settings.mobileNotificationsSaving") : "\u00a0")}
      </motion.p>
    </section>
  );
}
