import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { API } from "../api";
import type { Provider, Session } from "../types";
import { trackedFetch, useOpProgress } from "../progress/store";
import { cacheProviderModels, readProviderCache } from "../utils/providerCache";

interface Props {
  session: Session;
  providers: Provider[];
  disabled?: boolean;
  clientId?: string;
  onChange: (updates: Partial<Pick<Session, "provider_id" | "model" | "reasoning_effort" | "permission">>) => void;
  onSaved?: () => void;
}

interface ModelCatalog {
  models?: string[];
}

const providerModelsOp = (providerId: string) => `sessionSelector:models:${providerId}`;
const saveOp = (sessionId: string) => `sessionSelector:save:${sessionId}`;

function displayModel(model: string): string {
  if (!model) return "—";
  return model;
}

/** Per-session provider/model selector.
 *
 * Changing either field only mutates the Better Agent session metadata.
 * The backend starts a fresh provider subprocess lazily on the next prompt
 * when it detects that the stored provider/model no longer matches the last
 * active provider subprocess for this session.
 */
export function SessionSelectorControls({
  session,
  providers,
  disabled = false,
  clientId,
  onChange,
  onSaved,
}: Props) {
  const { t } = useTranslation();
  const [models, setModels] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const selectedProviderId = session.provider_id || providers.find((p) => !p.suspended)?.id || "";
  const saving = useOpProgress(saveOp(session.id)).inflight;
  const loadingModels = useOpProgress(selectedProviderId ? providerModelsOp(selectedProviderId) : "").inflight;
  const busy = disabled || saving;

  useEffect(() => {
    let cancelled = false;
    setError(null);
    if (!selectedProviderId) {
      setModels([]);
      return;
    }
    const cached = readProviderCache()?.modelsByProvider[selectedProviderId] ?? [];
    setModels(cached);
    trackedFetch(providerModelsOp(selectedProviderId), `${API}/api/providers/${encodeURIComponent(selectedProviderId)}/models`)
      .then((r) => r.json() as Promise<ModelCatalog>)
      .then((catalog) => {
        if (cancelled) return;
        const list = catalog.models || [];
        cacheProviderModels(selectedProviderId, list);
        setModels(list);
      })
      .catch((e) => {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [selectedProviderId]);

  const save = async (
    updates: Partial<Pick<Session, "provider_id" | "model" | "reasoning_effort" | "permission">>,
    optimistic: Partial<Pick<Session, "provider_id" | "model" | "reasoning_effort" | "permission">>,
  ) => {
    setError(null);
    const prev = {
      provider_id: session.provider_id,
      model: session.model,
      reasoning_effort: session.reasoning_effort,
      permission: session.permission,
    };
    onChange(optimistic);
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
      onSaved?.();
    } catch (e) {
      onChange(prev);
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const changeProvider = (providerId: string) => {
    const nextProvider = providers.find((p) => p.id === providerId && !p.suspended);
    if (!nextProvider || providerId === selectedProviderId) return;
    const cachedModels = readProviderCache()?.modelsByProvider[providerId] ?? [];
    const preferredModel =
      nextProvider.last_model || nextProvider.default_model || cachedModels[0] || "";
    if (!preferredModel) {
      setError(t("sessionSelector.noModelForProvider", "No model is available for this provider."));
      return;
    }
    const optimistic: Partial<Pick<Session, "provider_id" | "model" | "reasoning_effort" | "permission">> = {
      provider_id: providerId,
      model: preferredModel,
      reasoning_effort: nextProvider.default_reasoning_effort || "",
      permission: nextProvider.default_permission || {},
    };
    void save(
      {
        provider_id: providerId,
        model: preferredModel,
      },
      optimistic,
    );
  };

  const changeModel = (model: string) => {
    if (!model || model === session.model) return;
    void save({ model }, { model });
  };

  const modelOptions = useMemo(() => {
    const seen = new Set<string>();
    const out: string[] = [];
    for (const item of [session.model, ...models]) {
      if (!item || seen.has(item)) continue;
      seen.add(item);
      out.push(item);
    }
    return out;
  }, [session.model, models]);

  if (!providers.length) return null;

  return (
    <div
      className="session-selector-controls"
      title={t(
        "chat.sessionSelectorsHint",
        "Change this session’s provider/model. The next prompt continues in a fresh provider subprocess if needed.",
      )}
    >
      <label className="session-selector-control">
        <span>{t("newSession.provider", "Provider")}</span>
        <select
          value={selectedProviderId}
          disabled={busy}
          onChange={(e) => changeProvider(e.target.value)}
          aria-label={t("newSession.provider", "Provider")}
        >
          {providers.map((p) => (
            <option key={p.id} value={p.id} disabled={p.suspended}>
              {p.name}{p.suspended ? ` — ${t("setup.suspended", "Suspended")}` : ""}
            </option>
          ))}
        </select>
      </label>
      <label className="session-selector-control session-selector-control--model">
        <span>{t("newSession.model", "Model")}</span>
        <select
          value={session.model || ""}
          disabled={busy || loadingModels}
          onChange={(e) => changeModel(e.target.value)}
          aria-label={t("newSession.model", "Model")}
        >
          {modelOptions.length ? (
            modelOptions.map((m) => <option key={m} value={m}>{displayModel(m)}</option>)
          ) : (
            <option value={session.model || ""}>{displayModel(session.model)}</option>
          )}
        </select>
      </label>
      {saving || loadingModels ? <span className="session-selector-status">…</span> : null}
      {error ? <span className="session-selector-error" title={error}>!</span> : null}
    </div>
  );
}
