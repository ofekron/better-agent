import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { HarnessProfile } from "../../types";
import { isItemOverridden, settingState, toggleState, userInstructionsState } from "./resolve";
import {
  GROUP_INSTRUCTIONS,
  GROUP_SETTINGS,
  GROUP_USER_INSTRUCTIONS,
  SCOPE_GLOBAL,
  groupTitleKey,
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
}: GroupProps & { item: HarnessDescriptorItem }) {
  const { t } = useTranslation();
  const state = toggleState(profile, group, item, extensionId);
  const isGlobal = group.scope === SCOPE_GLOBAL;
  const path = extensionId === null
    ? [group.id, item.name]
    : ["extension_instances", extensionId, group.id, item.name];
  const confirm = useGlobalConfirm(isGlobal && !isDefault, (value) => onWrite({ path, value }));
  const locked = (item.locked_by ?? []).length > 0;

  return (
    <div className={`harness-item-row ${state.overridden ? "is-overridden" : ""}`}>
      <label className="harness-item-toggle">
        <input
          type="checkbox"
          checked={state.effective}
          disabled={disabled || locked}
          onChange={(e) => confirm.request(e.target.checked)}
        />
        <span className="harness-item-label">{item.label}</span>
      </label>
      {item.description && <span className="harness-item-description">{item.description}</span>}
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
      {item.description && <span className="harness-item-description">{item.description}</span>}
      {!isDefault && (
        <StateBadge
          overridden={state.overridden}
          disabled={disabled}
          onReset={() => onWrite({ path, clear: true })}
        />
      )}
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
        <span className="harness-item-description">
          {group.items.map((item) => item.label).join(", ")}
        </span>
      </div>
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

  return (
    <div className="harness-group">
      <div className="harness-group-title">
        {t(groupTitleKey(group.id))}
        {group.scope === SCOPE_GLOBAL && <GlobalBadge />}
      </div>
      {body}
    </div>
  );
}

export { StateBadge };
