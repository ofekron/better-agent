import type {
  HarnessProfile,
  HarnessProfileDelta,
  HarnessProfileListFieldView,
} from "../../types";
import {
  GROUP_SETTINGS,
  GROUP_USER_INSTRUCTIONS,
  INVERTED_GROUPS,
  SCOPE_GLOBAL,
  type HarnessDescriptorGroup,
  type HarnessDescriptorItem,
} from "./types";

/** What one control shows: its effective value for the selected profile, and
 * whether that value comes from this profile's own override or is inherited
 * from Default. */
export interface RowState<T = unknown> {
  effective: T;
  overridden: boolean;
}

function inDelta(delta: HarnessProfileDelta | null, name: string): boolean {
  return !!delta && (delta.add.includes(name) || delta.remove.includes(name));
}

function listView(
  profile: HarnessProfile,
  extensionId: string | null,
  groupId: string,
): HarnessProfileListFieldView | undefined {
  if (extensionId === null) {
    return (profile.fields as Record<string, unknown>)[groupId] as HarnessProfileListFieldView | undefined;
  }
  const instance = profile.fields.extension_instances[extensionId] as unknown as
    | Record<string, HarnessProfileListFieldView>
    | undefined;
  return instance?.[groupId];
}

/** Name under which an extension's free-text user instructions appear as an
 * instruction source. Must match `harness_fields.user_instruction_source_name`. */
export function userInstructionSourceName(extensionId: string): string {
  return `${extensionId} user instructions`;
}

export function toggleState(
  profile: HarnessProfile,
  group: HarnessDescriptorGroup,
  item: HarnessDescriptorItem,
  extensionId: string | null,
): RowState<boolean> {
  // A global field has one value for everyone; the descriptor already
  // carries the live one, and no profile can override it.
  if (group.scope === SCOPE_GLOBAL) {
    return { effective: !!item.default_enabled, overridden: false };
  }
  const view = listView(profile, extensionId, group.id);
  if (!view) return { effective: !!item.default_enabled, overridden: false };
  const present = view.resolved.includes(item.name);
  // Inverted groups store a disable-list, so membership means "off".
  const effective = INVERTED_GROUPS.has(group.id) ? !present : present;
  return { effective, overridden: inDelta(view.override, item.name) };
}

export function settingState(
  profile: HarnessProfile,
  extensionId: string,
  item: HarnessDescriptorItem,
): RowState {
  const entry = profile.fields.extension_instances[extensionId]?.setting_overlays?.[item.name];
  if (!entry) return { effective: item.default_value, overridden: false };
  return {
    effective: (entry.resolved as { value: unknown } | null)?.value,
    overridden: entry.override !== null,
  };
}

export function userInstructionsState(
  profile: HarnessProfile,
  extensionId: string,
): RowState<string> {
  const entry = profile.fields.instruction_sources[userInstructionSourceName(extensionId)];
  if (!entry) return { effective: "", overridden: false };
  return {
    effective: String(entry.resolved?.content ?? ""),
    overridden: entry.override !== null,
  };
}

/** Whether one control in a group is overridden on this profile. Drives the
 * "Diff vs Default" filter, which selects items before rendering so a
 * fully-inherited group disappears instead of rendering an empty shell. */
export function isItemOverridden(
  profile: HarnessProfile,
  group: HarnessDescriptorGroup,
  item: HarnessDescriptorItem,
  extensionId: string | null,
): boolean {
  if (group.scope === SCOPE_GLOBAL) return false;
  if (group.id === GROUP_SETTINGS && extensionId) {
    return !item.secret && settingState(profile, extensionId, item).overridden;
  }
  return toggleState(profile, group, item, extensionId).overridden;
}

/** Whether a group carries any override on this profile — drives the
 * "Diff vs Default" filter and the per-profile override count. */
export function groupOverrideCount(
  profile: HarnessProfile,
  group: HarnessDescriptorGroup,
  extensionId: string | null,
): number {
  if (group.scope === SCOPE_GLOBAL) return 0;
  if (group.id === GROUP_USER_INSTRUCTIONS && extensionId) {
    return userInstructionsState(profile, extensionId).overridden ? 1 : 0;
  }
  if (group.id === GROUP_SETTINGS && extensionId) {
    return group.items.filter(
      (item) => !item.secret && settingState(profile, extensionId, item).overridden,
    ).length;
  }
  const view = listView(profile, extensionId, group.id);
  const override = view?.override;
  if (!override) return 0;
  return group.items.filter((item) => inDelta(override, item.name)).length;
}

/** Every override this profile holds, as clear-writes — backs "Reset all". */
export function clearAllWrites(
  profile: HarnessProfile,
  groups: { group: HarnessDescriptorGroup; extensionId: string | null }[],
): { path: string[]; clear: true }[] {
  const writes: { path: string[]; clear: true }[] = [];
  for (const { group, extensionId } of groups) {
    if (groupOverrideCount(profile, group, extensionId) === 0) continue;
    if (extensionId === null) {
      /* c8 ignore start -- a non-extension group only reaches here when
       * groupOverrideCount > 0, which for a top-level list group requires a
       * non-empty items list; group.items[0]?.name is therefore always
       * defined and the `?? ""` fallback is statically unreachable. */
      writes.push({ path: [group.id, group.items[0]?.name ?? ""], clear: true });
      /* c8 ignore stop */
      continue;
    }
    if (group.id === GROUP_SETTINGS) {
      for (const item of group.items) {
        if (!item.secret && settingState(profile, extensionId, item).overridden) {
          writes.push({ path: ["extension_instances", extensionId, group.id, item.name], clear: true });
        }
      }
      continue;
    }
    writes.push({ path: ["extension_instances", extensionId, group.id, group.items[0]?.name ?? ""], clear: true });
  }
  return writes;
}
