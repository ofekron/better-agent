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
  description: string;
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
  required: boolean;
  enabled: boolean;
  runtime_ready: boolean;
  runtime_not_ready_reason: string;
  groups: HarnessDescriptorGroup[];
}

export interface HarnessDescriptor {
  extensions: HarnessDescriptorExtension[];
  builtin_tools: HarnessDescriptorGroup;
  builtin_extensions: HarnessDescriptorGroup;
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
export const GROUP_DISABLED_BUILTIN_TOOLS = "disabled_builtin_tools";
export const GROUP_DISABLED_BUILTIN_EXTENSIONS = "disabled_builtin_extensions";

/** Groups whose stored field is a disable-list, so the toggle shown to the
 * user ("available") is the inverse of the stored membership. */
export const INVERTED_GROUPS: ReadonlySet<string> = new Set([
  GROUP_DISABLED_BUILTIN_TOOLS,
  GROUP_DISABLED_BUILTIN_EXTENSIONS,
]);

/** i18n key for a group's heading. */
export function groupTitleKey(groupId: string): string {
  return `harnessProfile.group.${groupId}`;
}
