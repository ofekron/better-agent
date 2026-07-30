import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Select } from "./Select";
import { API } from "../api";
import { trackPromise } from "../progress/store";
import type { Provider, RuntimeProfile } from "../types";
import { useRuntimeProfiles } from "../hooks/useRuntimeProfiles";
import { effortsForRunner } from "./modelPicker";

/** Per-task runtime-profile assignment for the backend's internal LLM calls
 * (requirement analysis, config-sync review, search/Ask worker,
 * project-structure edit, and the default for new sessions). Every field is
 * optional — an unset profile inherits the default runtime profile at
 * resolve time, and unset model/effort inherit the assigned profile's
 * defaults, so the unconfigured state is never a hardcode. */
type Assignment = {
  runtime_profile_id?: string;
  model?: string;
  reasoning_effort?: string;
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
  const { snapshot, profiles, defaultProfileId } = useRuntimeProfiles();
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
        .then((data: { providers?: Provider[] }) => {
          setProviders(data.providers || []);
        }),
    ).promise.catch(() => {});
  }, [settingsEndpoint]);

  const providerById = useMemo(() => {
    const m: Record<string, Provider> = {};
    for (const p of providers) m[p.id] = p;
    return m;
  }, [providers]);

  const profileById = useMemo(() => {
    const m: Record<string, RuntimeProfile> = {};
    for (const p of snapshot?.runtime_profiles ?? []) m[p.id] = p;
    return m;
  }, [snapshot]);

  const effectiveProfile = (task: string): RuntimeProfile | undefined => {
    const a = assignments[task];
    const id = (a && a.runtime_profile_id) || defaultProfileId || "";
    return id ? profileById[id] : undefined;
  };

  const effortOptionsFor = (profile: RuntimeProfile | undefined) => {
    if (!profile) return [];
    const provider = providerById[profile.provider_id];
    return provider ? effortsForRunner(provider, profile.runner) : [];
  };

  const change = async (task: string, field: keyof Assignment, value: string) => {
    const next: Record<string, Assignment> = { ...assignments };
    const entry: Assignment = { ...(next[task] || {}) };
    if (value === INHERIT) delete entry[field];
    else entry[field] = value;
    if (field === "runtime_profile_id") {
      // The overrides are profile-scoped: re-validate them against the newly
      // effective profile, dropping what no longer applies.
      const id = value === INHERIT ? defaultProfileId || "" : value;
      const profile = id ? profileById[id] : undefined;
      const opts = effortOptionsFor(profile);
      if (entry.reasoning_effort && !opts.includes(entry.reasoning_effort as never)) {
        delete entry.reasoning_effort;
      }
      delete entry.model;
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
        const profile = effectiveProfile(task);
        const provider = profile ? providerById[profile.provider_id] : undefined;
        const effortOptions = effortOptionsFor(profile);
        const modelSet = new Set<string>();
        if (a.model) modelSet.add(a.model);
        if (profile?.default_model) modelSet.add(profile.default_model);
        const lastModel = profile ? snapshot?.last_models?.[profile.id] : "";
        if (lastModel) modelSet.add(lastModel);
        for (const m of provider?.custom_models || []) modelSet.add(m);
        const modelOptions = Array.from(modelSet);
        // Assigned-but-tombstoned profile: keep it visible (badged) so the
        // stored value stays truthful; the backend resolves it to the
        // default profile until reassigned.
        const assignedTombstone = a.runtime_profile_id
          ? profileById[a.runtime_profile_id]?.deleted_at
            ? profileById[a.runtime_profile_id]
            : undefined
          : undefined;
        return (
          <div key={task} className="internal-llm-row">
            <div className="internal-llm-task">{taskLabel(task)}</div>
            <label className="context-strategy-row">
              <span>{t("settings.internalLlmProfile")}</span>
              <Select
                value={a.runtime_profile_id || INHERIT}
                disabled={saving}
                onChange={(v) => void change(task, "runtime_profile_id", v)}
                options={[
                  { value: INHERIT, label: t("settings.internalLlmInherit") },
                  ...(assignedTombstone
                    ? [{
                        value: assignedTombstone.id,
                        label: `${assignedTombstone.name} — ${t("runtimeProfile.deletedBadge")}`,
                      }]
                    : []),
                  ...profiles.map((p) => ({ value: p.id, label: p.name })),
                ]}
              />
            </label>
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
