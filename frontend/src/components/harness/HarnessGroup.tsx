import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { HarnessProfile } from "../../types";
import { isItemOverridden, settingState, toggleState, userInstructionsState } from "./resolve";
import {
  GROUP_INSTRUCTIONS,
  GROUP_SETTINGS,
  GROUP_USER_INSTRUCTIONS,
  SCOPE_GLOBAL,
  groupHintKey,
  groupTitleKey,
  itemHintKey,
  itemLabelKey,
  type HarnessDescriptorExtension,
  type HarnessDescriptorGroup,
  type HarnessDescriptorItem,
  type HarnessFieldWrite,
} from "./types";

interface GroupProps {
  group: HarnessDescriptorGroup;
  profile: HarnessProfile;
  /** null for the top-level builtin-tool/extension groups. */
  extensionId: string | null;
  isDefault: boolean;
  disabled: boolean;
  /** Filters the tree to overridden controls only. */
  diffOnly: boolean;
  onWrite: (write: HarnessFieldWrite) => void;
  onOpenDescription?: (path: string, label: string) => void;
}

export function DescriptionLink({
  description,
  path,
  label,
  onOpen,
  className,
}: {
  description: string;
  path?: string;
  label: string;
  onOpen?: (path: string, label: string) => void;
  className: string;
}) {
  if (!description) return null;
  if (!path || !onOpen) return <p className={className}>{description}</p>;
  return (
    <button
      type="button"
      className={`${className} harness-description-link`}
      onClick={() => onOpen(path, label)}
    >
      {description}
    </button>
  );
}

/** Inherit/override state with a reset affordance. Absent on Default, which
 * has nothing above it to inherit from. */
function StateBadge({
  overridden,
  onReset,
  disabled,
}: {
  overridden: boolean;
  onReset: () => void;
  disabled: boolean;
}) {
  const { t } = useTranslation();
  return (
    <span className={`harness-field-badge ${overridden ? "overridden" : "inherited"}`}>
      {overridden ? t("harnessProfile.overrideBadge") : t("harnessProfile.inheritedBadge")}
      {overridden && (
        <button type="button" className="harness-field-reset" disabled={disabled} onClick={onReset}>
          {t("harnessProfile.resetToDefault")}
        </button>
      )}
    </span>
  );
}

/** A manifest-scoped instruction section only reaches the provider kinds it
 * names, so the toggle alone would overstate what enabling it does. */
function ProviderScope({ providers }: { providers?: string[] }) {
  const { t } = useTranslation();
  if (!providers?.length) return null;
  return (
    <span className="harness-item-detail harness-item-providers">
      {t("harnessProfile.providerScope", { providers: providers.join(", ") })}
    </span>
  );
}

function GlobalBadge() {
  const { t } = useTranslation();
  return <span className="harness-field-badge global">{t("harnessProfile.globalBadge")}</span>;
}

/** Global fields have one value for every profile. Editing one from a named
 * profile is legitimate but reaches further than the profile, so it is
 * confirmed rather than applied silently. */
function useGlobalConfirm(needsConfirm: boolean, apply: (value: unknown) => void) {
  const [pending, setPending] = useState<{ value: unknown } | null>(null);
  return {
    pending,
    cancel: () => setPending(null),
    confirm: () => {
      if (pending) apply(pending.value);
      setPending(null);
    },
    request: (value: unknown) => {
      if (!needsConfirm) {
        apply(value);
        return;
      }
      setPending({ value });
    },
  };
}

function ToggleRow({
  group,
  item,
  profile,
  extensionId,
  isDefault,
  disabled,
  onWrite,
  onOpenDescription,
  labelOverride,
}: GroupProps & { item: HarnessDescriptorItem; labelOverride?: string }) {
  const { t } = useTranslation();
  const state = toggleState(profile, group, item, extensionId);
  const isGlobal = group.scope === SCOPE_GLOBAL;
  const path = extensionId === null
    ? [group.id, item.name]
    : ["extension_instances", extensionId, group.id, item.name];
  const confirm = useGlobalConfirm(isGlobal && !isDefault, (value) => onWrite({ path, value }));
  const locked = (item.locked_by ?? []).length > 0;
  const labelKey = itemLabelKey(group.id, item.name);
  const hintKey = itemHintKey(group.id, item.name);
  // The extension's own words win; the built-in hint covers the fixed
  // vocabularies (permissions, UI surfaces) that no extension describes.
  const caption = item.description || (hintKey ? t(hintKey, { defaultValue: "" }) : "");

  return (
    <div className={`harness-item-row ${state.overridden ? "is-overridden" : ""}`}>
      <label className="harness-item-toggle">
        <input
          type="checkbox"
          checked={state.effective}
          disabled={disabled || locked}
          onChange={(e) => confirm.request(e.target.checked)}
        />
        <span className="harness-item-label">
          {labelOverride ?? (labelKey ? t(labelKey, { defaultValue: item.label }) : item.label)}
        </span>
      </label>
      {item.detail && <span className="harness-item-detail">{item.detail}</span>}
      <ProviderScope providers={item.providers} />
      {locked && (
        <span className="harness-item-locked">
          {t("harnessProfile.lockedBy", { holders: (item.locked_by ?? []).join(", ") })}
        </span>
      )}
      {isGlobal ? (
        <GlobalBadge />
      ) : (
        !isDefault && (
          <StateBadge
            overridden={state.overridden}
            disabled={disabled}
            // Item toggles store one delta for the whole leaf, so resetting
            // an item is writing Default's value back: the recomputed delta
            // simply no longer mentions it.
            onReset={() => onWrite({ path, value: !!item.default_enabled })}
          />
        )
      )}
      {confirm.pending && (
        <span className="harness-global-confirm">
          {t("harnessProfile.globalConfirmPrompt")}
          <button type="button" className="btn-secondary" onClick={confirm.confirm}>
            {t("harnessProfile.globalConfirmApply")}
          </button>
          <button type="button" className="btn-secondary" onClick={confirm.cancel}>
            {t("harnessProfile.globalConfirmCancel")}
          </button>
        </span>
      )}
      <DescriptionLink
        description={caption}
        path={item.description ? item.description_path : undefined}
        label={item.label}
        onOpen={onOpenDescription}
        className="harness-item-hint"
      />
    </div>
  );
}

function SettingRow({
  item,
  profile,
  extensionId,
  isDefault,
  disabled,
  onWrite,
  onOpenDescription,
}: GroupProps & { item: HarnessDescriptorItem; extensionId: string }) {
  const { t } = useTranslation();
  const state = settingState(profile, extensionId, item);
  const path = ["extension_instances", extensionId, GROUP_SETTINGS, item.name];
  const text = state.effective == null ? "" : String(state.effective);

  // A secret never becomes profile state: it is written straight to the OS
  // keychain and reported only as present or absent.
  if (item.secret) {
    return (
      <div className="harness-item-row">
        <span className="harness-item-label">{item.label}</span>
        <input
          type="password"
          className="harness-setting-input"
          placeholder={item.secret_present ? t("harnessProfile.secretStored") : t("harnessProfile.secretEmpty")}
          disabled={disabled}
          onBlur={(e) => {
            if (e.target.value) {
              onWrite({ path, value: e.target.value });
              e.target.value = "";
            }
          }}
        />
        <GlobalBadge />
        <DescriptionLink
          description={item.description}
          path={item.description_path}
          label={item.label}
          onOpen={onOpenDescription}
          className="harness-item-hint"
        />
      </div>
    );
  }

  const isBoolean = item.type === "boolean";
  const options = item.enum ?? [];
  return (
    <div className={`harness-item-row ${state.overridden ? "is-overridden" : ""}`}>
      <span className="harness-item-label">{item.label}</span>
      {isBoolean ? (
        <input
          type="checkbox"
          checked={!!state.effective}
          disabled={disabled}
          onChange={(e) => onWrite({ path, value: e.target.checked })}
        />
      ) : options.length > 0 ? (
        <select
          className="harness-setting-input"
          value={String(state.effective ?? "")}
          disabled={disabled}
          onChange={(e) => onWrite({ path, value: e.target.value })}
        >
          {options.map((option) => (
            <option key={option} value={option}>{option}</option>
          ))}
        </select>
      ) : (
        <input
          // Keyed on the effective value so a backend change remounts the
          // field with the new value instead of stranding a stale draft.
          key={text}
          type={item.type === "number" ? "number" : "text"}
          className="harness-setting-input"
          defaultValue={text}
          disabled={disabled}
          onBlur={(e) => {
            if (e.target.value === text) return;
            onWrite({ path, value: item.type === "number" ? Number(e.target.value) : e.target.value });
          }}
        />
      )}
      {!isDefault && (
        <StateBadge
          overridden={state.overridden}
          disabled={disabled}
          onReset={() => onWrite({ path, clear: true })}
        />
      )}
      <DescriptionLink
        description={item.description}
        path={item.description_path}
        label={item.label}
        onOpen={onOpenDescription}
        className="harness-item-hint"
      />
    </div>
  );
}

function UserInstructionsRow({
  profile,
  extensionId,
  isDefault,
  disabled,
  onWrite,
}: GroupProps & { extensionId: string }) {
  const { t } = useTranslation();
  const state = userInstructionsState(profile, extensionId);
  const path = ["extension_instances", extensionId, GROUP_USER_INSTRUCTIONS];

  return (
    <div className={`harness-item-row is-block ${state.overridden ? "is-overridden" : ""}`}>
      <div className="harness-item-row-header">
        <span className="harness-item-label">{t("harnessProfile.userInstructionsLabel")}</span>
        {!isDefault && (
          <StateBadge
            overridden={state.overridden}
            disabled={disabled}
            onReset={() => onWrite({ path, clear: true })}
          />
        )}
      </div>
      <textarea
        key={state.effective}
        className="harness-instructions-input"
        rows={3}
        defaultValue={state.effective}
        disabled={disabled}
        placeholder={t("harnessProfile.userInstructionsPlaceholder")}
        onBlur={(e) => {
          if (e.target.value !== state.effective) onWrite({ path, value: e.target.value });
        }}
      />
    </div>
  );
}

export function HarnessGroup(props: GroupProps) {
  const { t } = useTranslation();
  const { group, extensionId, isDefault, profile, disabled, diffOnly, onWrite } = props;

  // Default toggles instruction injection for the whole extension; a named
  // profile selects individual sections. The descriptor states which, so the
  // split is data-driven rather than a hardcoded special case.
  const instructionsAtExtensionLevel =
    group.id === GROUP_INSTRUCTIONS && isDefault && group.default_granularity === "extension";

  // Selecting visible items up front (rather than letting each row bail out)
  // keeps a fully-inherited group from rendering as an empty shell.
  const visibleItems = diffOnly
    ? group.items.filter((item) => isItemOverridden(profile, group, item, extensionId))
    : group.items;

  let body: React.ReactNode;
  if (group.id === GROUP_USER_INSTRUCTIONS && extensionId) {
    if (diffOnly && !userInstructionsState(profile, extensionId).overridden) return null;
    body = <UserInstructionsRow {...props} extensionId={extensionId} />;
  } else if (instructionsAtExtensionLevel) {
    if (diffOnly) return null;
    body = (
      <div className="harness-item-row">
        <label className="harness-item-toggle">
          <input
            type="checkbox"
            checked={!!group.value}
            disabled={disabled}
            onChange={(e) =>
              onWrite({
                path: ["extension_instances", extensionId ?? "", group.id, ""],
                value: e.target.checked,
              })
            }
          />
          <span className="harness-item-label">{t("harnessProfile.injectInstructions")}</span>
        </label>
      </div>
    );
    // The single toggle covers every section, but the user still needs to see
    // WHICH rules that turns on — list each section with what it says.
    body = (
      <>
        {body}
        {group.items.map((item) => (
          <div key={item.name} className="harness-item-row harness-instruction-section">
            <span className="harness-item-label">{item.label}</span>
            {item.detail && <span className="harness-item-detail">{item.detail}</span>}
            <ProviderScope providers={item.providers} />
            <DescriptionLink
              description={item.description}
              path={item.description_path}
              label={item.label}
              onOpen={props.onOpenDescription}
              className="harness-item-hint"
            />
          </div>
        ))}
      </>
    );
  } else if (group.id === GROUP_SETTINGS && extensionId) {
    if (!visibleItems.length) return null;
    body = visibleItems.map((item) => (
      <SettingRow key={item.name} {...props} item={item} extensionId={extensionId} />
    ));
  } else {
    if (!visibleItems.length) return null;
    body = visibleItems.map((item) => <ToggleRow key={item.name} {...props} item={item} />);
  }

  const hint = t(groupHintKey(group.id), { defaultValue: "" });

  return (
    <div className="harness-group">
      <div className="harness-group-title">
        {t(groupTitleKey(group.id))}
        {group.scope === SCOPE_GLOBAL && <GlobalBadge />}
      </div>
      {hint && <p className="harness-group-hint">{hint}</p>}
      {body}
    </div>
  );
}

/** The profile-scoped "is this extension live here" switch. It is stored in
 * the top-level disabled_builtin_extensions list, but it belongs on the
 * extension's own card: enablement is what gates every other control there,
 * so splitting it into a separate section hid the master switch from the
 * settings it governs. Path stays top-level, hence extensionId={null}. */
export function ExtensionEnabledToggle({
  group,
  extensionId,
  ...rest
}: Omit<GroupProps, "extensionId"> & { extensionId: string }) {
  const { t } = useTranslation();
  const item = group.items.find((candidate) => candidate.name === extensionId);
  if (!item) return null;
  if (rest.diffOnly && !isItemOverridden(rest.profile, group, item, null)) return null;
  return (
    <div className="harness-extension-enabled">
      <ToggleRow
        {...rest}
        group={group}
        extensionId={null}
        item={item}
        labelOverride={t("harnessProfile.extensionEnabled")}
      />
    </div>
  );
}

/** Everything below the master switch is inert while the extension is off in
 * this profile: its tools, skills and instructions do not reach the agent, so
 * live-looking controls would misrepresent backend state. */
export function ExtensionGroups({
  extension,
  enabledGroup,
  ...rest
}: Omit<GroupProps, "group" | "extensionId"> & {
  extension: HarnessDescriptorExtension;
  enabledGroup: HarnessDescriptorGroup;
}) {
  const { t } = useTranslation();
  const enabledItem = enabledGroup.items.find((candidate) => candidate.name === extension.id);
  const enabled = enabledItem
    ? toggleState(rest.profile, enabledGroup, enabledItem, null).effective
    : true;
  return (
    <div
      className={`harness-extension-groups ${enabled ? "" : "is-extension-off"}`}
      aria-disabled={!enabled}
    >
      {!enabled && (
        <p className="harness-extension-off-note">{t("harnessProfile.extensionOffNote")}</p>
      )}
      {extension.groups.map((group) => (
        <HarnessGroup
          key={group.id}
          {...rest}
          group={group}
          extensionId={extension.id}
          disabled={rest.disabled || !enabled}
        />
      ))}
    </div>
  );
}

export { StateBadge };
