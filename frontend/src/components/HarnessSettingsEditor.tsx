import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { API } from "../api";
import { eventBus } from "../lib/eventBus";
import { trackedFetch } from "../progress/store";
import type {
  HarnessProfile,
  HarnessProfileDelta,
  HarnessProfileExtensionInstanceView,
  HarnessProfileFieldView,
  HarnessProfileListFieldView,
  HarnessProfileOverrideOp,
} from "../types";
import { HarnessProfileSelector } from "./HarnessProfileSelector";

const FILE_EDIT_EXTENSION_ID = "ofek-dev.file-edit";

/** Thrown when the backend rejects a write because the caller's revision is
 * stale (another tab/session mutated the profile first). Distinguished from
 * a generic failure so the UI can point the user at a refetch instead of a
 * raw HTTP error. */
export const REVISION_MISMATCH = "revision_mismatch";

async function throwForStatus(res: Response): Promise<never> {
  if (res.status === 409) throw new Error(REVISION_MISMATCH);
  throw new Error(`HTTP ${res.status}`);
}

async function fetchProfile(id: string): Promise<HarnessProfile> {
  const res = await trackedFetch(`harnessProfiles:fetch:${id}`, `${API}/api/harness-profiles/${encodeURIComponent(id)}`);
  if (!res.ok) return throwForStatus(res);
  return res.json();
}

async function patchOverrides(id: string, ops: HarnessProfileOverrideOp[], revision: string): Promise<HarnessProfile> {
  const res = await fetch(`${API}/api/harness-profiles/${encodeURIComponent(id)}/overrides`, {
    method: "PATCH",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ revision, ops }),
  });
  if (!res.ok) return throwForStatus(res);
  return res.json();
}

async function patchExtension(path: string, body: unknown): Promise<void> {
  const res = await fetch(`${API}${path}`, {
    method: "PATCH",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
}

async function putDisabledBuiltin(kind: "disabled-builtin-tools" | "disabled-builtin-extensions", value: string[]): Promise<void> {
  const key = kind === "disabled-builtin-tools" ? "disabled_builtin_tools" : "disabled_builtin_extensions";
  const res = await fetch(`${API}/api/harness/default/${kind}`, {
    method: "PUT",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ [key]: value }),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
}

/** Small badge showing whether a leaf is inherited from Default or has a
 * local override, with a "Reset to Default" affordance for overridden
 * leaves on named profiles. */
function FieldBadge({
  overridden,
  onReset,
  resetDisabled,
}: {
  overridden: boolean;
  onReset?: () => void;
  resetDisabled?: boolean;
}) {
  const { t } = useTranslation();
  return (
    <span className={`harness-field-badge ${overridden ? "overridden" : "inherited"}`}>
      {overridden ? t("harnessProfile.overrideBadge") : t("harnessProfile.inheritedBadge")}
      {overridden && onReset ? (
        <button type="button" className="harness-field-reset" disabled={resetDisabled} onClick={onReset}>
          {t("harnessProfile.resetToDefault")}
        </button>
      ) : null}
    </span>
  );
}

/** Comma-separated chip list editor bound directly to one side (add/remove)
 * of a HarnessProfileDelta. */
function DeltaListInput({
  label,
  values,
  disabled,
  onChange,
}: {
  label: string;
  values: string[];
  disabled: boolean;
  onChange: (next: string[]) => void;
}) {
  const [text, setText] = useState(values.join(", "));
  useEffect(() => setText(values.join(", ")), [values]);
  return (
    <label className="harness-delta-input">
      <span>{label}</span>
      <input
        type="text"
        value={text}
        disabled={disabled}
        onChange={(e) => setText(e.target.value)}
        onBlur={() => onChange(text.split(",").map((s) => s.trim()).filter(Boolean))}
      />
    </label>
  );
}

/** One list-shaped leaf (mcp_servers / skills / instruction_names /
 * disabled_builtin_tools / disabled_builtin_extensions). Default view is
 * read-only chips of the live resolved set (editing Default routes through
 * the owning per-item extension endpoints elsewhere, or the two builtin
 * PUT endpoints for the two builtin fields). Named-profile view edits the
 * override delta directly. */
function ListFieldRow({
  label,
  field,
  isDefault,
  disabled,
  onSetOverride,
  onClear,
}: {
  label: string;
  field: HarnessProfileListFieldView;
  isDefault: boolean;
  disabled: boolean;
  onSetOverride: (delta: HarnessProfileDelta) => void;
  onClear: () => void;
}) {
  const overridden = field.override !== null;
  const delta = field.override ?? { add: [], remove: [] };
  return (
    <div className="harness-field-row">
      <div className="harness-field-row-header">
        <span className="harness-field-row-label">{label}</span>
        {!isDefault && <FieldBadge overridden={overridden} onReset={onClear} resetDisabled={disabled} />}
      </div>
      <div className="harness-field-resolved-chips">
        {field.resolved.length === 0 ? (
          <span className="harness-field-empty">—</span>
        ) : (
          field.resolved.map((item) => (
            <span key={item} className="harness-field-chip">{item}</span>
          ))
        )}
      </div>
      {!isDefault && (
        <div className="harness-delta-editor">
          <DeltaListInput
            label="+"
            values={delta.add}
            disabled={disabled}
            onChange={(add) => onSetOverride({ add, remove: delta.remove })}
          />
          <DeltaListInput
            label="−"
            values={delta.remove}
            disabled={disabled}
            onChange={(remove) => onSetOverride({ add: delta.add, remove })}
          />
        </div>
      )}
    </div>
  );
}

/** One boolean leaf (headless). */
function BooleanFieldRow({
  label,
  field,
  isDefault,
  disabled,
  onSet,
  onClear,
}: {
  label: string;
  field: HarnessProfileFieldView<boolean>;
  isDefault: boolean;
  disabled: boolean;
  onSet: (value: boolean) => void;
  onClear: () => void;
}) {
  const overridden = field.override !== null;
  const effective = overridden ? (field.override as boolean) : field.resolved;
  return (
    <div className="harness-field-row">
      <div className="harness-field-row-header">
        <span className="harness-field-row-label">{label}</span>
        {!isDefault && <FieldBadge overridden={overridden} onReset={onClear} resetDisabled={disabled} />}
      </div>
      <label className="harness-field-checkbox">
        <input
          type="checkbox"
          checked={effective}
          disabled={disabled}
          onChange={(e) => onSet(e.target.checked)}
        />
        {label}
      </label>
    </div>
  );
}

/** One extension's mcp/skills/instructions/settings/headless block. */
function ExtensionInstanceBlock({
  extensionId,
  view,
  isDefault,
  disabled,
  onSetListOverride,
  onClearListOverride,
  onSetSetting,
  onClearSetting,
  onSetHeadless,
  onClearHeadless,
}: {
  extensionId: string;
  view: HarnessProfileExtensionInstanceView;
  isDefault: boolean;
  disabled: boolean;
  onSetListOverride: (field: "mcp_servers" | "skills" | "instruction_names", delta: HarnessProfileDelta) => void;
  onClearListOverride: (field: "mcp_servers" | "skills" | "instruction_names") => void;
  onSetSetting: (key: string, value: unknown, schemaHash: string) => void;
  onClearSetting: (key: string) => void;
  onSetHeadless: (value: boolean) => void;
  onClearHeadless: () => void;
}) {
  const { t } = useTranslation();
  const settingEntries = Object.entries(view.setting_overlays);
  return (
    <div className="harness-extension-block">
      <div className="harness-extension-block-title">{extensionId}</div>
      <ListFieldRow
        label={t("harnessProfile.mcpServersField")}
        field={view.mcp_servers}
        isDefault={isDefault}
        disabled={disabled}
        onSetOverride={(delta) => onSetListOverride("mcp_servers", delta)}
        onClear={() => onClearListOverride("mcp_servers")}
      />
      <ListFieldRow
        label={t("harnessProfile.skillsField")}
        field={view.skills}
        isDefault={isDefault}
        disabled={disabled}
        onSetOverride={(delta) => onSetListOverride("skills", delta)}
        onClear={() => onClearListOverride("skills")}
      />
      <ListFieldRow
        label={t("harnessProfile.instructionNamesField")}
        field={view.instruction_names}
        isDefault={isDefault}
        disabled={disabled}
        onSetOverride={(delta) => onSetListOverride("instruction_names", delta)}
        onClear={() => onClearListOverride("instruction_names")}
      />
      {extensionId === "browserHarness" || extensionId.endsWith(".browser-harness") ? (
        <BooleanFieldRow
          label={t("harnessProfile.browserHarnessHeadlessLabel")}
          field={view.headless}
          isDefault={isDefault}
          disabled={disabled}
          onSet={onSetHeadless}
          onClear={onClearHeadless}
        />
      ) : null}
      {settingEntries.length > 0 && (
        <div className="harness-field-row">
          <div className="harness-field-row-header">
            <span className="harness-field-row-label">{t("harnessProfile.settingOverlaysField")}</span>
          </div>
          {settingEntries.map(([key, field]) => {
            const overridden = field.override !== null;
            const effective = overridden ? field.override!.value : field.resolved.value;
            const schemaHash = overridden ? field.override!.schema_hash : field.resolved.schema_hash;
            return (
              <div key={key} className="harness-setting-overlay-row">
                <span className="harness-setting-overlay-key">{key}</span>
                {!isDefault && <FieldBadge overridden={overridden} onReset={() => onClearSetting(key)} resetDisabled={disabled} />}
                {typeof effective === "boolean" ? (
                  <input
                    type="checkbox"
                    checked={effective}
                    disabled={disabled}
                    onChange={(e) => onSetSetting(key, e.target.checked, schemaHash)}
                  />
                ) : (
                  <input
                    type="text"
                    value={effective == null ? "" : String(effective)}
                    disabled={disabled}
                    onChange={(e) => onSetSetting(key, e.target.value, schemaHash)}
                  />
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

export function HarnessSettingsEditor() {
  const { t } = useTranslation();
  const [selectedId, setSelectedId] = useState("default");
  const [profile, setProfile] = useState<HarnessProfile | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [creating, setCreating] = useState(false);
  const [newProfileName, setNewProfileName] = useState("");

  const isDefault = selectedId === "default";

  const load = useCallback(() => {
    setLoading(true);
    fetchProfile(selectedId)
      .then((p) => {
        setProfile(p);
        setError("");
      })
      .catch((err) => {
        setProfile(null);
        setError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => setLoading(false));
  }, [selectedId]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    const unsubExtensions = eventBus.subscribe("extensions_changed", load);
    const unsubProfiles = eventBus.subscribe("harness_profiles_changed", load);
    return () => {
      unsubExtensions();
      unsubProfiles();
    };
  }, [load]);

  const runMutation = useCallback(
    (fn: () => Promise<void>) => {
      setSaving(true);
      setError("");
      fn()
        .then(() => load())
        .catch((err) => {
          const message = err instanceof Error ? err.message : String(err);
          setError(message);
          // A stale-revision rejection means another writer already moved
          // the profile forward — refetch so the editor converges on the
          // authoritative state instead of staying stuck on the stale one.
          if (message === REVISION_MISMATCH) load();
        })
        .finally(() => setSaving(false));
    },
    [load],
  );

  const applyOps = useCallback(
    (ops: HarnessProfileOverrideOp[]) => {
      if (!profile) return;
      runMutation(async () => {
        await patchOverrides(profile.id, ops, profile.revision);
      });
    },
    [profile, runMutation],
  );

  /** Single dispatcher for the two write shapes a field can take: on the
   * Default profile it writes straight to the live extension/config store;
   * on a named profile it patches the profile's overrides instead. */
  const writeField = useCallback(
    (writeDefault: () => Promise<void>, ops: HarnessProfileOverrideOp[]) => {
      if (isDefault) {
        runMutation(writeDefault);
        return;
      }
      applyOps(ops);
    },
    [isDefault, runMutation, applyOps],
  );

  const handleCreate = useCallback(() => {
    const name = newProfileName.trim();
    if (!name) return;
    setCreating(true);
    setError("");
    fetch(`${API}/api/harness-profiles`, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    })
      .then(async (res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const created: HarnessProfile = await res.json();
        setNewProfileName("");
        setSelectedId(created.id);
      })
      .catch((err) => setError(err instanceof Error ? err.message : String(err)))
      .finally(() => setCreating(false));
  }, [newProfileName]);

  const handleDelete = useCallback(() => {
    if (!profile || isDefault) return;
    setSaving(true);
    setError("");
    fetch(`${API}/api/harness-profiles/${encodeURIComponent(profile.id)}?revision=${encodeURIComponent(profile.revision)}`, {
      method: "DELETE",
      credentials: "include",
    })
      .then((res) => {
        if (!res.ok) return throwForStatus(res);
        setSelectedId("default");
      })
      .catch((err) => setError(err instanceof Error ? err.message : String(err)))
      .finally(() => setSaving(false));
  }, [profile, isDefault]);

  const disabled = saving || loading;

  const extensionEntries = useMemo(
    () => (profile ? Object.entries(profile.fields.extension_instances) : []),
    [profile],
  );

  if (loading && !profile) {
    return <div className="harness-settings-editor">{t("common.loading", "Loading…")}</div>;
  }

  return (
    <div className="harness-settings-editor">
      <div className="harness-settings-editor-toolbar">
        <HarnessProfileSelector
          value={selectedId}
          disabled={saving}
          className="harness-settings-profile-select"
          onChange={(id) => setSelectedId(id)}
        />
        <div className="harness-settings-create-row">
          <input
            type="text"
            placeholder={t("harnessProfile.createProfileNamePlaceholder")}
            value={newProfileName}
            disabled={creating}
            onChange={(e) => setNewProfileName(e.target.value)}
          />
          <button type="button" className="btn-secondary" disabled={creating || !newProfileName.trim()} onClick={handleCreate}>
            {t("harnessProfile.createProfile")}
          </button>
        </div>
        <button
          type="button"
          className="btn-danger"
          disabled={isDefault || disabled}
          title={isDefault ? t("harnessProfile.deleteDefaultBlocked") : undefined}
          onClick={handleDelete}
        >
          {t("harnessProfile.deleteProfile")}
        </button>
      </div>

      {error ? (
        <div className="harness-settings-editor-error">
          {error === REVISION_MISMATCH
            ? t("harnessProfile.revisionMismatch")
            : `${t("harnessProfile.patchError")}: ${error}`}
        </div>
      ) : null}

      {profile ? (
        <div className="harness-settings-editor-body">
          <div className="harness-extension-block">
            <div className="harness-extension-block-title">{t("harnessProfile.extensionInstancesTitle")}</div>
            {extensionEntries.map(([extensionId, view]) => (
              <ExtensionInstanceBlock
                key={extensionId}
                extensionId={extensionId}
                view={view}
                isDefault={isDefault}
                disabled={disabled}
                onSetListOverride={(field, delta) => {
                  if (isDefault) return;
                  applyOps([{ path: ["extension_instances", extensionId, field], op: "set", value: delta }]);
                }}
                onClearListOverride={(field) => {
                  if (isDefault) return;
                  applyOps([{ path: ["extension_instances", extensionId, field], op: "clear" }]);
                }}
                onSetSetting={(key, value, schemaHash) =>
                  writeField(
                    () => patchExtension(`/api/extensions/${encodeURIComponent(extensionId)}/settings`, { key, value }),
                    [{
                      path: ["extension_instances", extensionId, "setting_overlays", key],
                      op: "set",
                      value: { value, schema_hash: schemaHash },
                    }],
                  )
                }
                onClearSetting={(key) => {
                  if (isDefault) return;
                  applyOps([{ path: ["extension_instances", extensionId, "setting_overlays", key], op: "clear" }]);
                }}
                onSetHeadless={(value) =>
                  writeField(
                    () => patchExtension(`/api/extensions/${encodeURIComponent(extensionId)}/settings`, { key: "headless", value }),
                    [{ path: ["extension_instances", extensionId, "headless"], op: "set", value }],
                  )
                }
                onClearHeadless={() => {
                  if (isDefault) return;
                  applyOps([{ path: ["extension_instances", extensionId, "headless"], op: "clear" }]);
                }}
              />
            ))}
          </div>

          <div className="harness-extension-block">
            <div className="harness-extension-block-title">{t("harnessProfile.disabledBuiltinToolsTitle")}</div>
            <ListFieldRow
              label={t("harnessProfile.disabledBuiltinToolsTitle")}
              field={profile.fields.disabled_builtin_tools}
              isDefault={isDefault}
              disabled={disabled}
              onSetOverride={(delta) =>
                writeField(
                  () => putDisabledBuiltin("disabled-builtin-tools", delta.add),
                  [{ path: ["disabled_builtin_tools"], op: "set", value: delta }],
                )
              }
              onClear={() => {
                if (isDefault) return;
                applyOps([{ path: ["disabled_builtin_tools"], op: "clear" }]);
              }}
            />
          </div>

          <div className="harness-extension-block">
            <div className="harness-extension-block-title">{t("harnessProfile.disabledBuiltinExtensionsTitle")}</div>
            <ListFieldRow
              label={t("harnessProfile.disabledBuiltinExtensionsTitle")}
              field={profile.fields.disabled_builtin_extensions}
              isDefault={isDefault}
              disabled={disabled}
              onSetOverride={(delta) =>
                writeField(
                  () => putDisabledBuiltin("disabled-builtin-extensions", delta.add),
                  [{ path: ["disabled_builtin_extensions"], op: "set", value: delta }],
                )
              }
              onClear={() => {
                if (isDefault) return;
                applyOps([{ path: ["disabled_builtin_extensions"], op: "clear" }]);
              }}
            />
          </div>

          <div className="harness-extension-block">
            <div className="harness-extension-block-title">{t("harnessProfile.instructionSourcesTitle")}</div>
            {Object.entries(profile.fields.instruction_sources).map(([name, field]) => {
              const overridden = field.override !== null;
              const isFileEdit = field.resolved.extension_id === FILE_EDIT_EXTENSION_ID;
              return (
                <div key={name} className="harness-field-row">
                  <div className="harness-field-row-header">
                    <span className="harness-field-row-label">
                      {isFileEdit ? t("harnessProfile.fileEditExtensionLabel") : name}
                    </span>
                    {!isDefault && (
                      <FieldBadge
                        overridden={overridden}
                        resetDisabled={disabled}
                        onReset={() => applyOps([{ path: ["instruction_sources", name], op: "clear" }])}
                      />
                    )}
                  </div>
                  <div className="harness-instruction-source-kind">{field.resolved.kind}</div>
                </div>
              );
            })}
          </div>
        </div>
      ) : null}
    </div>
  );
}
