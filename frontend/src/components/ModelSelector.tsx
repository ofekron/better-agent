import { useState, useEffect, useRef, useCallback } from "react";
import Icon from "./Icon";
import { useTranslation } from "react-i18next";
import { ProgressButton } from "../progress/ProgressButton";
import { runThreeStateSync, trackedFetch } from "../progress/store";

import { API } from "../api";
import { useProviderChanged } from "../hooks/useProviderChanged";
import { useModelsCatalogChanged } from "../hooks/useModelsCatalogChanged";
const ADD_CUSTOM_MODEL_OP_ID = "provider:addCustomModel";

interface Props {
  value: string;
  onChange: (model: string) => void;
}

type FetchState = "warming" | "ok" | "failing";

interface Catalog {
  models: string[];
  retired: string[];
  last_fetch_state: FetchState;
  last_refreshed_at: number;
}

const EMPTY_CATALOG: Catalog = {
  models: [],
  retired: [],
  last_fetch_state: "ok",
  last_refreshed_at: 0,
};

export function ModelSelector({ value, onChange }: Props) {
  const { t } = useTranslation();
  const [catalog, setCatalog] = useState<Catalog>(EMPTY_CATALOG);
  const [isCustom, setIsCustom] = useState(false);
  const [customInput, setCustomInput] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  const refetch = useCallback(() => {
    trackedFetch("providers:models", `${API}/api/models`)
      .then((r) => r.json())
      .then((d) =>
        setCatalog({
          models: d.models || [],
          retired: d.retired || [],
          last_fetch_state: d.last_fetch_state || "ok",
          last_refreshed_at: d.last_refreshed_at || 0,
        }),
      )
      .catch(() => {});
  }, []);

  useEffect(() => {
    refetch();
  }, [refetch]);

  // Active provider can change at any time — refetch the model list
  // when the WS dispatches a provider_changed window event.
  useProviderChanged(refetch);
  // Per-provider catalog delta from the daily refresher (or manual refresh).
  useModelsCatalogChanged(refetch);

  const { models, retired, last_fetch_state: state } = catalog;
  const allKnown = [...models, ...retired];

  useEffect(() => {
    setIsCustom(value !== "" && !allKnown.includes(value));
    if (!allKnown.includes(value) && value) {
      setCustomInput(value);
    }
  }, [value, models, retired]);

  function handleAddCustom() {
    const name = customInput.trim();
    if (!name || allKnown.includes(name)) return;

    void runThreeStateSync({
      operationId: ADD_CUSTOM_MODEL_OP_ID,
      action: t("model.label"),
      reconcile: refetch,
      mutate: async () => {
        const r = await fetch(`${API}/api/providers/default/custom_models`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) });
        if (!r.ok) throw new Error("add custom model failed");
      },
    })
      .then(() => {
        setCatalog((prev) => ({ ...prev, models: [...prev.models, name] }));
        onChange(name);
        setIsCustom(false);
        setCustomInput("");
      })
      .catch(() => {});
  }

  return (
    <div className="model-selector">
      <label>{t("model.label")}</label>
      {state === "warming" && (
        <div className="model-selector-state model-selector-state-warming">
          {t("model.warming", "Loading models…")}
        </div>
      )}
      {state === "failing" && (
        <div className="model-selector-state model-selector-state-failing">
          {t(
            "model.failing",
            "Couldn't refresh models from the provider — showing last-known list.",
          )}
        </div>
      )}
      {!isCustom ? (
        <div style={{ display: "flex", gap: 4 }}>
          <select value={value} onChange={(e) => onChange(e.target.value)}>
            {models.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
            {retired.length > 0 && (
              <optgroup label={t("model.retiredGroup", "Retiring")}>
                {retired.map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
              </optgroup>
            )}
          </select>
          <button
            type="button"
            className="btn-icon"
            title={t("model.addCustomModelTitle")}
            onClick={() => {
              setIsCustom(true);
              setTimeout(() => inputRef.current?.focus(), 0);
            }}
          >
            +
          </button>
        </div>
      ) : (
        <div style={{ display: "flex", gap: 4 }}>
          <input
            ref={inputRef}
            value={customInput}
            onChange={(e) => setCustomInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") handleAddCustom();
              if (e.key === "Escape") {
                setIsCustom(false);
                setCustomInput("");
              }
            }}
            placeholder={t("model.modelNamePlaceholder")}
          />
          <ProgressButton
            opId={ADD_CUSTOM_MODEL_OP_ID}
            type="button"
            className="btn-icon"
            onClick={handleAddCustom}
          >
            ✓
          </ProgressButton>
          <button
            type="button"
            className="btn-icon"
            onClick={() => {
              setIsCustom(false);
              setCustomInput("");
            }}
          >
            <Icon name="x" size={14} />
          </button>
        </div>
      )}
    </div>
  );
}
