import { useTranslation } from "react-i18next";
import type { ReasoningEffort } from "../types";
import type { useProviderModelCatalog } from "../hooks/useProviderModelCatalog";
import { ModelCatalogStatus } from "./ModelCatalogStatus";
import { Select } from "./Select";

// Shared presentational fields for a runtime profile's defaults (model +
// reasoning effort). The catalog hook stays with the caller — the wizard and
// the management editor gate fetching differently.

type CatalogState = ReturnType<typeof useProviderModelCatalog>;

export function ModelDefaultField({
  catalogState,
  model,
  onModelChange,
}: {
  catalogState: CatalogState;
  model: string;
  onModelChange: (model: string) => void;
}) {
  const { t } = useTranslation();
  const { catalog, networkState, refresh, refreshing, refreshError } = catalogState;
  const models = catalog?.models ?? [];
  return (
    <div className="setup-field">
      <label>{t("runtimeProfile.defaultModelLabel")}</label>
      {models.length ? (
        <Select
          value={models.includes(model) ? model : ""}
          onChange={onModelChange}
          options={[
            ...(!models.includes(model)
              ? [{
                  value: "",
                  label: model
                    ? t("setup.defaultModelNotInList", { model })
                    : t("setup.defaultModelSelectPlaceholder"),
                  disabled: true,
                }]
              : []),
            ...models.map((m) => ({ value: m, label: m })),
          ]}
        />
      ) : (
        <input
          type="text"
          value={model}
          onChange={(e) => onModelChange(e.target.value)}
          placeholder={t("setup.defaultModelCustomPlaceholder")}
          spellCheck={false}
        />
      )}
      <ModelCatalogStatus
        catalog={catalog}
        networkState={networkState}
        onRefresh={refresh}
        refreshing={refreshing}
        refreshError={refreshError}
      />
    </div>
  );
}

export function EffortDefaultField({
  options,
  effort,
  onEffortChange,
}: {
  options: ReasoningEffort[];
  effort: ReasoningEffort | "";
  onEffortChange: (effort: ReasoningEffort) => void;
}) {
  const { t } = useTranslation();
  if (!options.length) return null;
  return (
    <div className="setup-field">
      <label>{t("runtimeProfile.defaultEffortLabel")}</label>
      <Select
        value={effort}
        onChange={(v) => onEffortChange(v as ReasoningEffort)}
        options={options.map((value) => ({
          value,
          label: t(`reasoningEffort.${value}`),
        }))}
      />
    </div>
  );
}
