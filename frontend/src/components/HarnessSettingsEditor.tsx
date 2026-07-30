import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { API } from "../api";
import { eventBus } from "../lib/eventBus";
import { trackedFetch } from "../progress/store";
import { ProgressButton } from "../progress/ProgressButton";
import { lazyWithRetry } from "../lib/lazyWithRetry";
import type { HarnessProfile } from "../types";
import {
  DescriptionLink,
  ExtensionEnabledToggle,
  ExtensionGroups,
  HarnessGroup,
} from "./harness/HarnessGroup";
import { HarnessProfileMeta } from "./harness/HarnessProfileMeta";
import {
  PROFILE_NOT_FOUND,
  REVISION_MISMATCH,
  createProfile,
  deleteProfile,
  fetchDescriptor,
  fetchProfile,
  writeFields,
} from "./harness/api";
import { clearAllWrites, groupOverrideCount } from "./harness/resolve";
import type {
  HarnessDescriptor,
  HarnessDescriptorGroup,
  HarnessFieldWrite,
} from "./harness/types";

const DEFAULT_ID = "default";

const FileViewer = lazyWithRetry(() =>
  import("./FileViewer").then((module) => ({ default: module.FileViewer })),
);

interface ProfileSummary {
  id: string;
  name: string;
  read_only?: boolean;
}

/** Every configurable group paired with the extension it belongs to (null
 * for the top-level builtin groups) — one flat list so override counts and
 * "reset all" don't re-walk the tree shape in three places. */
function allGroups(
  descriptor: HarnessDescriptor | null,
): { group: HarnessDescriptorGroup; extensionId: string | null }[] {
  if (!descriptor) return [];
  const groups: { group: HarnessDescriptorGroup; extensionId: string | null }[] = [];
  for (const extension of descriptor.extensions) {
    for (const group of extension.groups) groups.push({ group, extensionId: extension.id });
  }
  groups.push({ group: descriptor.builtin_tools, extensionId: null });
  groups.push({ group: descriptor.builtin_extensions, extensionId: null });
  groups.push({ group: descriptor.runtime_skills, extensionId: null });
  return groups;
}

interface HarnessSettingsEditorProps {
  onEditDescriptionFile?: (path: string) => Promise<unknown>;
}

export function HarnessSettingsEditor({ onEditDescriptionFile }: HarnessSettingsEditorProps) {
  const { t } = useTranslation();
  const [selectedId, setSelectedId] = useState(DEFAULT_ID);
  const [descriptor, setDescriptor] = useState<HarnessDescriptor | null>(null);
  const [profile, setProfile] = useState<HarnessProfile | null>(null);
  const [profiles, setProfiles] = useState<ProfileSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [creating, setCreating] = useState(false);
  const [newProfileName, setNewProfileName] = useState("");
  const [diffOnly, setDiffOnly] = useState(false);
  const [search, setSearch] = useState("");
  const [descriptionFile, setDescriptionFile] = useState<{ path: string; label: string } | null>(null);
  const localProfileWriteInFlight = useRef(false);
  const inFlightHarnessProfileEvents = useRef<Record<string, unknown>[]>([]);
  const inFlightExternalEvent = useRef(false);

  const isDefault = selectedId === DEFAULT_ID;
  const isReadOnly = Boolean(profile?.read_only);

  const loadProfiles = useCallback(() => {
    trackedFetch("harnessProfiles:list", `${API}/api/harness-profiles`)
      .then((res) => (res.ok ? res.json() : Promise.reject(new Error(`HTTP ${res.status}`))))
      .then((data: { profiles: HarnessProfile[] }) =>
        setProfiles((data.profiles || []).map((item) => ({ id: item.id, name: item.name, read_only: item.read_only }))),
      )
      .catch(() => setProfiles([{ id: DEFAULT_ID, name: t("harnessProfile.defaultOptionLabel") }]));
  }, [t]);

  const load = useCallback(() => {
    setLoading(true);
    Promise.all([fetchDescriptor(), fetchProfile(selectedId)])
      .then(([nextDescriptor, nextProfile]) => {
        setDescriptor(nextDescriptor);
        setProfile(nextProfile);
        setError("");
      })
      .catch((err) => {
        const message = err instanceof Error ? err.message : String(err);
        // The selection was deleted (here or in another tab) and the
        // profiles-changed refetch is chasing an id the backend no longer
        // has. Follow the backend down to Default rather than reporting a
        // dead id as a failure.
        if (message === PROFILE_NOT_FOUND) {
          setSelectedId(DEFAULT_ID);
          return;
        }
        setProfile(null);
        setError(message);
      })
      .finally(() => setLoading(false));
  }, [selectedId]);

  useEffect(() => {
    load();
    loadProfiles();
  }, [load, loadProfiles]);

  useEffect(() => {
    const reload = (payload: unknown, kind: "extensions" | "profiles") => {
      if (localProfileWriteInFlight.current) {
        if (kind === "profiles" && payload && typeof payload === "object") {
          inFlightHarnessProfileEvents.current.push(payload as Record<string, unknown>);
        } else {
          inFlightExternalEvent.current = true;
        }
        return;
      }
      load();
      loadProfiles();
    };
    // Default is a projection of extension state, so an extension change
    // moves it just as a profile write does — both channels refetch.
    const unsubExtensionConfig = eventBus.subscribe("extension.config", (payload) => reload(payload, "extensions"));
    const unsubDefaultHarness = eventBus.subscribe("extension.harness.default", (payload) => reload(payload, "extensions"));
    const unsubProfiles = eventBus.subscribe("harness_profiles_changed", (payload) => reload(payload, "profiles"));
    return () => {
      unsubExtensionConfig();
      unsubDefaultHarness();
      unsubProfiles();
    };
  }, [load, loadProfiles]);

  const runMutation = useCallback(
    (fn: () => Promise<HarnessProfile>) => {
      setSaving(true);
      setError("");
      localProfileWriteInFlight.current = true;
      fn()
        .then((nextProfile) => {
          setProfile(nextProfile);
          setError("");
          const events = inFlightHarnessProfileEvents.current;
          const sawOnlyLocalEcho =
            !inFlightExternalEvent.current &&
            events.length > 0 &&
            events.every((event) =>
              event.action === "fields_updated" &&
              event.profile_id === nextProfile.id &&
              event.revision === nextProfile.revision
            );
          if (!sawOnlyLocalEcho && (inFlightExternalEvent.current || events.length > 0)) {
            load();
            loadProfiles();
          }
        })
        .catch((err) => {
          const message = err instanceof Error ? err.message : String(err);
          // A write against a profile that no longer exists is the same
          // stale-selection case as above — reselect instead of erroring.
          if (message === PROFILE_NOT_FOUND) {
            setSelectedId(DEFAULT_ID);
            return;
          }
          setError(message);
          // A stale-revision rejection means another writer moved the
          // profile forward — refetch so the editor converges on the
          // authoritative state instead of staying stuck on the stale one.
          if (message === REVISION_MISMATCH) load();
        })
        .finally(() => {
          localProfileWriteInFlight.current = false;
          inFlightHarnessProfileEvents.current = [];
          inFlightExternalEvent.current = false;
          setSaving(false);
        });
    },
    [load],
  );

  const applyWrites = useCallback(
    (writes: HarnessFieldWrite[]) => {
      if (!profile || profile.read_only || !writes.length) return;
      runMutation(() => writeFields(profile.id, writes, profile.revision));
    },
    [profile, runMutation],
  );

  const handleCreate = useCallback(() => {
    const name = newProfileName.trim();
    if (!name) return;
    setCreating(true);
    setError("");
    createProfile(name)
      .then((created) => {
        setNewProfileName("");
        setSelectedId(created.id);
      })
      .catch((err) => setError(err instanceof Error ? err.message : String(err)))
      .finally(() => setCreating(false));
  }, [newProfileName]);

  const handleDelete = useCallback(() => {
    if (!profile || isDefault || profile.read_only) return;
    setSaving(true);
    setError("");
    deleteProfile(profile.id, profile.revision)
      .then(() => setSelectedId(DEFAULT_ID))
      .catch((err) => {
        const message = err instanceof Error ? err.message : String(err);
        // Already gone (another tab deleted it first) is the outcome this
        // click wanted, not a failure.
        if (message === PROFILE_NOT_FOUND) {
          setSelectedId(DEFAULT_ID);
          return;
        }
        setError(message);
      })
      .finally(() => setSaving(false));
  }, [profile, isDefault]);

  const groups = useMemo(() => allGroups(descriptor), [descriptor]);

  const overrideCount = useMemo(() => {
    if (!profile || isDefault) return 0;
    return groups.reduce(
      (total, entry) => total + groupOverrideCount(profile, entry.group, entry.extensionId),
      0,
    );
  }, [profile, groups, isDefault]);

  const visibleExtensions = useMemo(() => {
    if (!descriptor) return [];
    const needle = search.trim().toLowerCase();
    if (!needle) return descriptor.extensions;
    return descriptor.extensions.filter(
      (extension) =>
        extension.name.toLowerCase().includes(needle) || extension.id.toLowerCase().includes(needle),
    );
  }, [descriptor, search]);

  const disabled = saving || loading || isReadOnly;

  if (loading && !profile) {
    return <div className="harness-settings-editor">{t("common.loading", "Loading…")}</div>;
  }

  return (
    <div className="harness-settings-editor">
      <div className="harness-settings-layout">
        <nav className="harness-profile-rail" aria-label={t("settings.harnessProfilesSection")}>
          {profiles.map((item) => (
            <button
              key={item.id}
              type="button"
              className={`harness-profile-rail-item ${item.id === selectedId ? "is-selected" : ""}`}
              disabled={saving}
              onClick={() => setSelectedId(item.id)}
            >
              {item.id === DEFAULT_ID ? t("harnessProfile.defaultOptionLabel") : item.name}
            </button>
          ))}
          <div className="harness-settings-create-row">
            <input
              type="text"
              placeholder={t("harnessProfile.createProfileNamePlaceholder")}
              value={newProfileName}
              disabled={creating}
              onChange={(e) => setNewProfileName(e.target.value)}
            />
            <button
              type="button"
              className="btn-secondary"
              disabled={creating || !newProfileName.trim()}
              onClick={handleCreate}
            >
              {creating ? t("harnessProfile.creatingProfile") : t("harnessProfile.createProfile")}
            </button>
          </div>
        </nav>

        <section className="harness-settings-pane">
          <header className="harness-settings-header">
            <div className="harness-settings-header-title">
              {isDefault ? t("harnessProfile.defaultOptionLabel") : profile?.name}
              {saving && <span className="harness-settings-saving">{t("harnessProfile.saving")}</span>}
            </div>
            <p className="harness-settings-header-hint">
              {isDefault
                ? t("harnessProfile.defaultHint")
                : isReadOnly
                  ? t("harnessProfile.readOnlyHint")
                  : t("harnessProfile.overrideCount", { count: overrideCount })}
            </p>
            <div className="harness-settings-header-actions">
              <input
                type="search"
                className="harness-settings-search"
                placeholder={t("harnessProfile.searchExtensions")}
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
              {!isDefault && !isReadOnly && (
                <label className="harness-diff-toggle">
                  <input
                    type="checkbox"
                    checked={diffOnly}
                    onChange={(e) => setDiffOnly(e.target.checked)}
                  />
                  {t("harnessProfile.diffOnly")}
                </label>
              )}
              {!isDefault && !isReadOnly && (
                <button
                  type="button"
                  className="btn-secondary"
                  disabled={disabled || overrideCount === 0}
                  onClick={() => profile && applyWrites(clearAllWrites(profile, groups))}
                >
                  {t("harnessProfile.resetAll")}
                </button>
              )}
              <button
                type="button"
                className="btn-danger"
                disabled={isDefault || isReadOnly || disabled}
                title={
                  isDefault
                    ? t("harnessProfile.deleteDefaultBlocked")
                    : isReadOnly
                      ? t("harnessProfile.deleteReadOnlyBlocked")
                      : undefined
                }
                onClick={handleDelete}
              >
                {t("harnessProfile.deleteProfile")}
              </button>
            </div>
          </header>

          {error && (
            <div className="harness-settings-editor-error" role="alert">
              {error === REVISION_MISMATCH
                ? t("harnessProfile.revisionMismatch")
                : `${t("harnessProfile.patchError")}: ${error}`}
            </div>
          )}

          {profile && descriptor && (
            <div className="harness-settings-editor-body">
              {!isDefault && !isReadOnly && descriptor.profile_meta && (
                <HarnessProfileMeta
                  profile={profile}
                  profiles={profiles}
                  disabled={disabled}
                  onWrite={(write) => applyWrites([write])}
                />
              )}
              {visibleExtensions.map((extension) => (
                <article key={extension.id} className="harness-extension-block">
                  <div className="harness-extension-block-title">
                    {extension.name}
                    {!extension.runtime_ready && (
                      <span className="harness-extension-unavailable">
                        {t("harnessProfile.extensionUnavailable", {
                          reason: extension.runtime_not_ready_reason || t("harnessProfile.extensionDisabled"),
                        })}
                      </span>
                    )}
                  </div>
                  <DescriptionLink
                    description={extension.description}
                    path={extension.description_path}
                    label={extension.name}
                    onOpen={(path, label) => setDescriptionFile({ path, label })}
                    className="harness-extension-block-description"
                  />
                  <ExtensionEnabledToggle
                    group={descriptor.builtin_extensions}
                    profile={profile}
                    extensionId={extension.id}
                    isDefault={isDefault}
                    disabled={disabled}
                    diffOnly={diffOnly}
                    onWrite={(write) => applyWrites([write])}
                    onOpenDescription={(path, label) => setDescriptionFile({ path, label })}
                  />
                  <ExtensionGroups
                    extension={extension}
                    profile={profile}
                    enabledGroup={descriptor.builtin_extensions}
                    isDefault={isDefault}
                    disabled={disabled}
                    diffOnly={diffOnly}
                    onWrite={(write) => applyWrites([write])}
                    onOpenDescription={(path, label) => setDescriptionFile({ path, label })}
                  />
                </article>
              ))}

              <article className="harness-extension-block">
                <HarnessGroup
                  group={descriptor.builtin_tools}
                  profile={profile}
                  extensionId={null}
                  isDefault={isDefault}
                  disabled={disabled}
                  diffOnly={diffOnly}
                  onWrite={(write) => applyWrites([write])}
                  onOpenDescription={(path, label) => setDescriptionFile({ path, label })}
                />
                <HarnessGroup
                  group={descriptor.runtime_skills}
                  profile={profile}
                  extensionId={null}
                  isDefault={isDefault}
                  disabled={disabled || isDefault}
                  diffOnly={diffOnly}
                  onWrite={(write) => applyWrites([write])}
                  onOpenDescription={(path, label) => setDescriptionFile({ path, label })}
                />
              </article>
            </div>
          )}
        </section>
      </div>
      {descriptionFile && (
        <div className="harness-description-overlay" role="dialog" aria-modal="true" aria-label={descriptionFile.label}>
          <div className="harness-description-panel">
            <div className="harness-description-header">
              <span>{descriptionFile.label}</span>
              <div className="harness-description-actions">
                {onEditDescriptionFile && (
                  <ProgressButton
                    opId={`fileEditor:start:${descriptionFile.path}`}
                    className="btn-secondary"
                    onClick={() => void onEditDescriptionFile(descriptionFile.path)}
                    loadingChildren={t("common.loading", "Loading…")}
                  >
                    {t("files.editWithAiTitle")}
                  </ProgressButton>
                )}
                <button
                  type="button"
                  className="project-settings-close"
                  onClick={() => setDescriptionFile(null)}
                  aria-label={t("common.close", "Close")}
                >
                  &times;
                </button>
              </div>
            </div>
            <div className="harness-description-file-panel">
              <Suspense
                fallback={
                  <div className="harness-settings-editor">
                    {t("common.loading", "Loading…")}
                  </div>
                }
              >
                <FileViewer filePath={descriptionFile.path} onClose={() => setDescriptionFile(null)} />
              </Suspense>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
