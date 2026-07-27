/** Mirrors `backend/harness_fields.py` — the one description of what a
 * harness profile can configure. The UI renders its control tree from this
 * so a new extension setting needs no frontend change. */

export const SCOPE_PROFILE = "profile";
export const SCOPE_GLOBAL = "global";

export type HarnessScope = typeof SCOPE_PROFILE | typeof SCOPE_GLOBAL;

export type HarnessControl = "item_toggles" | "settings" | "text" | "instructions";

export interface HarnessDescriptorItem {
  name: string;
  label: string;
  /** What the item does, in the extension's own words — from its manifest or
   * from the artifact it ships (a skill's SKILL.md, an instruction block's
   * opening paragraph). Empty when the extension declares nothing. */
  description: string;
  /** Absolute validated path of the artifact that owns `description`. */
  description_path?: string;
  /** Short classification chip (instruction level, native kind, module slot,
   * required/optional) — a label, never an explanation. */
  detail?: string;
  /** Instruction items only: the provider kinds this section is limited to.
   * Empty means every provider. */
  providers?: string[];
  /** Live global state — what Default resolves to for this item. */
  default_enabled?: boolean;
  /** Non-empty when the item can't be toggled, naming what holds it. */
  locked_by?: string[];
  // Settings-group items only.
  type?: string;
  enum?: string[];
  scope?: HarnessScope;
  secret?: boolean;
  secret_present?: boolean;
  default_value?: unknown;
  schema_hash?: string;
}

export interface HarnessDescriptorGroup {
  id: string;
  scope: HarnessScope;
  control: HarnessControl;
  items: HarnessDescriptorItem[];
  value: unknown;
  /** Instructions only: Default toggles the whole extension, a named
   * profile selects individual sections. */
  default_granularity?: string;
  profile_granularity?: string;
}

export interface HarnessDescriptorExtension {
  id: string;
  name: string;
  description: string;
  /** Absolute validated path of the manifest that owns `description`. */
  description_path?: string;
  required: boolean;
  enabled: boolean;
  runtime_ready: boolean;
  runtime_not_ready_reason: string;
  groups: HarnessDescriptorGroup[];
}

/** Scalar profile-meta fields (base pointer + provider/model/effort pins) —
 * not deltas over Default, rendered by a dedicated widget. */
export interface HarnessProfileMetaDescriptor {
  id: string;
  scope: HarnessScope;
  fields: { name: string; kind: string }[];
}

export interface HarnessDescriptor {
  extensions: HarnessDescriptorExtension[];
  builtin_tools: HarnessDescriptorGroup;
  builtin_extensions: HarnessDescriptorGroup;
  runtime_skills: HarnessDescriptorGroup;
  profile_meta?: HarnessProfileMetaDescriptor;
}

/** One field write. `clear` reverts a named profile's override back to
 * inheriting Default; it is rejected on Default and on global fields. */
export interface HarnessFieldWrite {
  path: string[];
  value?: unknown;
  clear?: boolean;
}

export const GROUP_MCP = "mcp_servers";
export const GROUP_SKILLS = "skills";
export const GROUP_INSTRUCTIONS = "instruction_names";
export const GROUP_SETTINGS = "setting_overlays";
export const GROUP_USER_INSTRUCTIONS = "user_instructions";
export const GROUP_UI_SURFACES = "ui_surfaces";
export const GROUP_PERMISSIONS = "permissions";
export const GROUP_DISABLED_BUILTIN_TOOLS = "disabled_builtin_tools";
export const GROUP_DISABLED_BUILTIN_EXTENSIONS = "disabled_builtin_extensions";
export const GROUP_DISABLED_RUNTIME_SKILLS = "disabled_runtime_skills";

/** Groups whose stored field is a disable-list, so the toggle shown to the
 * user ("available") is the inverse of the stored membership. */
export const INVERTED_GROUPS: ReadonlySet<string> = new Set([
  GROUP_DISABLED_BUILTIN_TOOLS,
  GROUP_DISABLED_BUILTIN_EXTENSIONS,
  GROUP_DISABLED_RUNTIME_SKILLS,
]);

/** i18n key for a group's heading. */
export function groupTitleKey(groupId: string): string {
  return `harnessProfile.group.${groupId}`;
}

/** i18n key for the plain-language explanation of what a group controls. */
export function groupHintKey(groupId: string): string {
  return `harnessProfile.groupHint.${groupId}`;
}

/** Groups whose item names are a fixed vocabulary the UI can explain by name.
 * Everything else names extension-authored items, whose text comes from the
 * manifest via the descriptor instead. */
const EXPLAINED_ITEM_GROUPS: ReadonlySet<string> = new Set([
  GROUP_PERMISSIONS,
  GROUP_UI_SURFACES,
]);

/** i18n key for an item's explanation, or null when the group's items are
 * extension-authored and carry their own descriptor text. */
export function itemHintKey(groupId: string, itemName: string): string | null {
  if (!EXPLAINED_ITEM_GROUPS.has(groupId)) return null;
  return `harnessProfile.itemHint.${groupId}.${itemName}`;
}

/** i18n key for an item's display label, or null to use the descriptor label. */
export function itemLabelKey(groupId: string, itemName: string): string | null {
  if (groupId !== GROUP_UI_SURFACES) return null;
  return `harnessProfile.itemLabel.${groupId}.${itemName}`;
}
