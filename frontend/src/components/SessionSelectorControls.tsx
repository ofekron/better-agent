import { useState } from "react";
import { useTranslation } from "react-i18next";
import { API } from "../api";
import type { Provider, Session } from "../types";
import { trackedFetch, useOpProgress } from "../progress/store";
import { ModelPickerModal } from "./ModelPickerModal";
import type { SelectorUpdates } from "./modelPicker";

interface Props {
  session: Session;
  providers: Provider[];
  disabled?: boolean;
  clientId?: string;
  onChange: (updates: SelectorUpdates) => void;
  onSaved?: () => void;
}

const saveOp = (sessionId: string) => `sessionSelector:save:${sessionId}`;

export function SessionSelectorControls({
  session,
  providers,
  disabled = false,
  clientId,
  onChange,
  onSaved,
}: Props) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const saving = useOpProgress(saveOp(session.id)).inflight;
  const busy = disabled || saving;

  const save = async (updates: SelectorUpdates) => {
    setError(null);
    const prev = {
      provider_id: session.provider_id,
      model: session.model,
      reasoning_effort: session.reasoning_effort,
      permission: session.permission,
    };
    onChange(updates);
    try {
      const r = await trackedFetch(
        saveOp(session.id),
        `${API}/api/sessions/${encodeURIComponent(session.id)}/selectors`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ...updates, client_id: clientId }),
        },
      );
      const body = await r.json().catch(() => null) as { updates?: Partial<Session> } | null;
      if (body?.updates) onChange(body.updates);
      setOpen(false);
      onSaved?.();
    } catch (e) {
      onChange(prev);
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const selectedProviderId = session.provider_id || providers.find((p) => !p.suspended)?.id || "";
  const selectedProvider = providers.find((p) => p.id === selectedProviderId);
  const selectorSummary = [selectedProvider?.name, session.model].filter(Boolean).join(" / ");

  if (!providers.length) return null;

  return (
    <div
      className="session-selector-controls"
      title={t(
        "chat.sessionSelectorsHint",
        "Change this session's provider/model. The next prompt continues in a fresh provider subprocess if needed.",
      )}
    >
      <button
        type="button"
        className="session-selector-picker-button"
        onClick={() => !busy && setOpen(true)}
        disabled={busy}
        aria-label={t("sessionSelector.openPicker", "Change session model")}
      >
        <span>{selectorSummary || t("sessionSelector.openPicker", "Change session model")}</span>
      </button>
      {saving ? <span className="session-selector-status">...</span> : null}
      {error && !open ? <span className="session-selector-error" title={error || ""}>!</span> : null}
      {open ? (
        <ModelPickerModal
          session={session}
          providers={providers}
          disabled={disabled}
          saving={saving}
          onConfirm={(updates) => void save(updates)}
          onClose={() => setOpen(false)}
        />
      ) : null}
    </div>
  );
}
