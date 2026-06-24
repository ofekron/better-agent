import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { API } from "../api";
import { trackPromise } from "../progress/store";
import type { Provider } from "../types";

/** Per-task assignment of provider + model + reasoning effort for the
 * backend's internal LLM calls (requirement analysis, config-sync review,
 * search/Ask worker, project-structure edit, and the default for new
 * sessions). Every field is optional — an unset field inherits from the
 * active provider at resolve time, so the unconfigured state is never a
 * hardcode. */
type Assignment = {
  provider_id?: string;
  model?: string;
  reasoning_effort?: string;
};

const INHERIT = "";

export function InternalLLMSetting() {
  const { t } = useTranslation();
  const [tasks, setTasks] = useState<string[]>([]);
  const [assignments, setAssignments] = useState<Record<string, Assignment>>({});
  const [providers, setProviders] = useState<Provider[]>([]);
  const [defaultProviderId, setDefaultProviderId] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    trackPromise("internalLlm:load", () =>
      fetch(`${API}/api/settings/internal-llm`)
        .then((r) => r.json())
        .then((data: { tasks?: string[]; assignments?: Record<string, Assignment> }) => {
          setTasks(data.tasks || []);
          setAssignments(data.assignments || {});
        }),
    ).promise.catch(() => {});
    trackPromise("internalLlm:providers", () =>
      fetch(`${API}/api/providers`)
        .then((r) => r.json())
        .then((data: { providers?: Provider[]; default_provider_id?: string | null }) => {
          setProviders(data.providers || []);
          setDefaultProviderId(data.default_provider_id ?? null);
        }),
    ).promise.catch(() => {});
  }, []);

  const providerById = useMemo(() => {
    const m: Record<string, Provider> = {};
    for (const p of providers) m[p.id] = p;
    return m;
  }, [providers]);

  const effectiveProvider = (task: string): Provider | undefined => {
    const a = assignments[task];
    const id = (a && a.provider_id) || defaultProviderId || "";
    return id ? providerById[id] : undefined;
  };

  const change = async (task: string, field: keyof Assignment, value: string) => {
    const next: Record<string, Assignment> = { ...assignments };
    const entry: Assignment = { ...(next[task] || {}) };
    if (value === INHERIT) delete entry[field];
    else (entry[field] as string) = value;
    // Drop an effort value that the resolved provider no longer supports.
    if (field === "provider_id") {
      const id = value === INHERIT ? defaultProviderId || "" : value;
      const p = id ? providerById[id] : undefined;
      const opts = p?.reasoning_effort_options || [];
      if (entry.reasoning_effort && opts && !opts.includes(entry.reasoning_effort as never)) {
        delete entry.reasoning_effort;
      }
    }
    if (Object.keys(entry).length === 0) delete next[task];
    else next[task] = entry;
    setAssignments(next);
    setSaving(true);
    try {
      await trackPromise("internalLlm:save", () =>
        fetch(`${API}/api/settings/internal-llm`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ assignments: next }),
        }),
      ).promise;
    } catch {
      return;
    } finally {
      setSaving(false);
    }
  };

  const taskLabel = (task: string) => t(`settings.internalLlmTask.${task}`, task);

  return (
    <div className="internal-llm-setting">
      <div className="context-strategy-hint">{t("settings.internalLlmHint")}</div>
      {tasks.map((task) => {
        const a = assignments[task] || {};
        const provider = effectiveProvider(task);
        const effortOptions = provider?.supports_reasoning_effort
          ? provider.reasoning_effort_options || []
          : [];
        const modelSet = new Set<string>();
        if (provider?.default_model) modelSet.add(provider.default_model);
        for (const m of provider?.custom_models || []) modelSet.add(m);
        const modelOptions = Array.from(modelSet);
        return (
          <div key={task} className="internal-llm-row">
            <div className="internal-llm-task">{taskLabel(task)}</div>
            <label className="context-strategy-row">
              <span>{t("settings.internalLlmProvider")}</span>
              <select
                value={a.provider_id || INHERIT}
                disabled={saving}
                onChange={(e) => void change(task, "provider_id", e.target.value)}
              >
                <option value={INHERIT}>{t("settings.internalLlmInherit")}</option>
                {providers.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
            </label>
            <label className="context-strategy-row">
              <span>{t("settings.internalLlmModel")}</span>
              <select
                value={a.model || INHERIT}
                disabled={saving}
                onChange={(e) => void change(task, "model", e.target.value)}
              >
                <option value={INHERIT}>{t("settings.internalLlmInherit")}</option>
                {modelOptions.map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
              </select>
            </label>
            {effortOptions.length > 0 && (
              <label className="context-strategy-row">
                <span>{t("settings.internalLlmEffort")}</span>
                <select
                  value={a.reasoning_effort || INHERIT}
                  disabled={saving}
                  onChange={(e) => void change(task, "reasoning_effort", e.target.value)}
                >
                  <option value={INHERIT}>{t("settings.internalLlmInherit")}</option>
                  {effortOptions.map((e2) => (
                    <option key={e2} value={e2}>
                      {e2}
                    </option>
                  ))}
                </select>
              </label>
            )}
          </div>
        );
      })}
    </div>
  );
}
