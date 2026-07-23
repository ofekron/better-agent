import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Select } from "./Select";
import { API } from "../api";
import { trackPromise } from "../progress/store";
import type { Provider } from "../types";
import { effortsForRunner, runnerForProvider, runnerLabelKey } from "./modelPicker";

/** Per-task runtime profile assignment for the
 * backend's internal LLM calls (requirement analysis, config-sync review,
 * search/Ask worker, project-structure edit, and the default for new
 * sessions). Every field is optional — an unset field inherits from the
 * active provider at resolve time, so the unconfigured state is never a
 * hardcode. */
type Assignment = {
  provider_id?: string;
  model?: string;
  reasoning_effort?: string;
  runner?: Provider["runner"];
};

const INHERIT = "";

interface InternalLLMSettingProps {
  tasks?: string[];
  showHint?: boolean;
  extensionId?: string;
}

export function InternalLLMSetting({ tasks: taskOverride, showHint = true, extensionId = "" }: InternalLLMSettingProps = {}) {
  const { t } = useTranslation();
  const [loadedTasks, setLoadedTasks] = useState<string[]>([]);
  const [assignments, setAssignments] = useState<Record<string, Assignment>>({});
  const [providers, setProviders] = useState<Provider[]>([]);
  const [defaultProviderId, setDefaultProviderId] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const settingsEndpoint = extensionId
    ? `${API}/api/extensions/${encodeURIComponent(extensionId)}/internal-llm`
    : `${API}/api/settings/internal-llm`;

  useEffect(() => {
    trackPromise("internalLlm:load", () =>
      fetch(settingsEndpoint)
        .then((r) => r.json())
        .then((data: { tasks?: string[]; assignments?: Record<string, Assignment> }) => {
          setLoadedTasks(data.tasks || []);
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
  }, [settingsEndpoint]);

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
    if (field === "provider_id" || field === "runner") {
      const id = value === INHERIT ? defaultProviderId || "" : value;
      const p = field === "provider_id" ? (id ? providerById[id] : undefined) : effectiveProvider(task);
      if (field === "provider_id" && entry.runner && p && !p.runner_options.includes(entry.runner)) {
        delete entry.runner;
      }
      const runner = field === "runner" && value !== INHERIT
        ? value as Provider["runner"]
        : p ? runnerForProvider(p) : "native";
      const opts = p ? effortsForRunner(p, runner) : [];
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
        fetch(settingsEndpoint, {
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
  const tasks = taskOverride ?? loadedTasks;
  if (tasks.length === 0) return null;

  return (
    <div className="internal-llm-setting">
      {showHint && <div className="context-strategy-hint">{t("settings.internalLlmHint")}</div>}
      {tasks.map((task) => {
        const a = assignments[task] || {};
        const provider = effectiveProvider(task);
        const runner = a.runner && provider?.runner_options.includes(a.runner)
          ? a.runner
          : provider ? runnerForProvider(provider) : "native";
        const effortOptions = provider ? effortsForRunner(provider, runner) : [];
        const modelSet = new Set<string>();
        if (provider?.default_model) modelSet.add(provider.default_model);
        for (const m of provider?.custom_models || []) modelSet.add(m);
        const modelOptions = Array.from(modelSet);
        return (
          <div key={task} className="internal-llm-row">
            <div className="internal-llm-task">{taskLabel(task)}</div>
            <label className="context-strategy-row">
              <span>{t("settings.internalLlmProvider")}</span>
              <Select
                value={a.provider_id || INHERIT}
                disabled={saving}
                onChange={(v) => void change(task, "provider_id", v)}
                options={[
                  { value: INHERIT, label: t("settings.internalLlmInherit") },
                  ...providers.map((p) => ({ value: p.id, label: p.name })),
                ]}
              />
            </label>
            {provider && provider.runner_options.length > 1 && (
              <label className="context-strategy-row session-runtime-axis">
                <span>{t("newSession.runner")}</span>
                <Select
                  value={a.runner || INHERIT}
                  disabled={saving}
                  onChange={(v) => void change(task, "runner", v)}
                  options={[
                    { value: INHERIT, label: t("settings.internalLlmInherit") },
                    ...provider.runner_options.map((value) => ({
                      value,
                      label: t(runnerLabelKey(provider.kind, value)),
                    })),
                  ]}
                />
              </label>
            )}
            <label className="context-strategy-row">
              <span>{t("settings.internalLlmModel")}</span>
              <Select
                value={a.model || INHERIT}
                disabled={saving}
                onChange={(v) => void change(task, "model", v)}
                options={[
                  { value: INHERIT, label: t("settings.internalLlmInherit") },
                  ...modelOptions.map((m) => ({ value: m, label: m })),
                ]}
              />
            </label>
            {effortOptions.length > 0 && (
              <label className="context-strategy-row">
                <span>{t("settings.internalLlmEffort")}</span>
                <Select
                  value={a.reasoning_effort || INHERIT}
                  disabled={saving}
                  onChange={(v) => void change(task, "reasoning_effort", v)}
                  options={[
                    { value: INHERIT, label: t("settings.internalLlmInherit") },
                    ...effortOptions.map((e2) => ({ value: e2, label: e2 })),
                  ]}
                />
              </label>
            )}
          </div>
        );
      })}
    </div>
  );
}
