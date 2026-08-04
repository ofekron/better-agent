import { describe, expect, it, vi } from "vitest";
import { act, fireEvent, render } from "@testing-library/react";

// react-i18next: return the key by default, but honor `defaultValue` (so the
// groupHint arm renders "" for unseeded groups and a real hint for the one
// seeded group — exercising both branches of `hint && <p>`).
vi.mock("react-i18next", () => {
  const t = (k: string, opts?: Record<string, unknown>) => {
    if (k === "harnessProfile.groupHint.permissions") return "Perms hint";
    if (opts && "defaultValue" in opts) return opts.defaultValue as string;
    return k;
  };
  return { useTranslation: () => ({ t }) };
});

import {
  DescriptionLink,
  ExtensionEnabledToggle,
  ExtensionGroups,
  HarnessGroup,
  StateBadge,
} from "../src/components/harness/HarnessGroup";
import {
  GROUP_INSTRUCTIONS,
  GROUP_SETTINGS,
  GROUP_USER_INSTRUCTIONS,
} from "../src/components/harness/types";
import type {
  HarnessDescriptorExtension,
  HarnessDescriptorGroup,
  HarnessDescriptorItem,
} from "../src/components/harness/types";
import type { HarnessProfile } from "../src/types";

// --- fixtures ----------------------------------------------------------------

function listView(resolved: string[] = [], override: { add: string[]; remove: string[] } | null = null) {
  return { resolved, override };
}

function emptyProfile(): HarnessProfile {
  return {
    id: "p1",
    name: "P",
    description: "",
    revision: "r",
    created_at: "",
    updated_at: "",
    fields: {
      extension_instances: {},
      disabled_builtin_tools: listView(),
      disabled_builtin_extensions: listView(),
      disabled_runtime_skills: listView(),
      instruction_sources: {},
    },
    mcp_overrides: {},
    skill_overrides: {},
    native_harness_overrides: {},
    provider_run_config_overlay: {},
  } as unknown as HarnessProfile;
}

/** Put a list-field override on a TOP-LEVEL (extensionId=null) group. */
function topLevelOverride(profile: HarnessProfile, groupId: string, itemName: string) {
  (profile.fields as Record<string, unknown>)[groupId] = listView([itemName], {
    add: [itemName],
    remove: [],
  });
}

function extInstance(profile: HarnessProfile, extId: string) {
  const fields = profile.fields as unknown as {
    extension_instances: Record<string, Record<string, unknown>>;
  };
  const inst = (fields.extension_instances[extId] ??= {
    mcp_servers: listView(),
    skills: listView(),
    instruction_names: listView(),
    setting_overlays: {},
    headless: { resolved: false, override: null },
  });
  return inst;
}

function toggleItem(label: string, over: Partial<HarnessDescriptorItem> = {}): HarnessDescriptorItem {
  return {
    name: label,
    label,
    description: "",
    default_enabled: false,
    ...over,
  };
}

function group(
  id: string,
  items: HarnessDescriptorItem[],
  over: Partial<HarnessDescriptorGroup> = {},
): HarnessDescriptorGroup {
  return {
    id,
    scope: "profile",
    control: "item_toggles",
    items,
    value: null,
    ...over,
  };
}

interface RenderOpts {
  extensionId?: string | null;
  isDefault?: boolean;
  disabled?: boolean;
  diffOnly?: boolean;
  profile?: HarnessProfile;
  onOpenDescription?: (path: string, label: string) => void;
}

function renderGroup(g: HarnessDescriptorGroup, opts: RenderOpts = {}) {
  const profile = opts.profile ?? emptyProfile();
  const onWrite = vi.fn();
  const onOpenDescription = opts.onOpenDescription ?? vi.fn();
  const utils = render(
    <HarnessGroup
      group={g}
      profile={profile}
      extensionId={opts.extensionId ?? null}
      isDefault={opts.isDefault ?? false}
      disabled={opts.disabled ?? false}
      diffOnly={opts.diffOnly ?? false}
      onWrite={onWrite}
      onOpenDescription={onOpenDescription}
    />,
  );
  return { ...utils, onWrite, onOpenDescription, profile };
}

// --- DescriptionLink ---------------------------------------------------------

describe("DescriptionLink", () => {
  it("renders nothing when there is no description", () => {
    const { container } = render(<DescriptionLink description="" path="/p" label="l" onOpen={vi.fn()} className="c" />);
    expect(container.textContent).toBe("");
  });

  it("renders a plain paragraph when path or onOpen is missing", () => {
    const { container } = render(<DescriptionLink description="d" label="l" className="c" />);
    expect(container.querySelector("p.c")?.textContent).toBe("d");
    expect(container.querySelector("button")).toBeNull();
  });

  it("renders a paragraph when only onOpen is present (no path)", () => {
    const { container } = render(<DescriptionLink description="d" onOpen={vi.fn()} label="l" className="c" />);
    expect(container.querySelector("p.c")?.textContent).toBe("d");
  });

  it("renders a button that opens the description when path and onOpen are present", () => {
    const onOpen = vi.fn();
    const { container } = render(<DescriptionLink description="d" path="/p" label="lbl" onOpen={onOpen} className="c" />);
    const btn = container.querySelector("button.harness-description-link")!;
    expect(btn.textContent).toBe("d");
    fireEvent.click(btn);
    expect(onOpen).toHaveBeenCalledWith("/p", "lbl");
  });
});

// --- StateBadge --------------------------------------------------------------

describe("StateBadge", () => {
  it("renders the inherited badge and no reset when not overridden", () => {
    const { container } = render(<StateBadge overridden={false} onReset={vi.fn()} disabled={false} />);
    expect(container.textContent).toContain("harnessProfile.inheritedBadge");
    expect(container.querySelector(".harness-field-reset")).toBeNull();
  });

  it("renders the override badge, a reset button, and fires onReset", () => {
    const onReset = vi.fn();
    const { container } = render(<StateBadge overridden={true} onReset={onReset} disabled={false} />);
    expect(container.textContent).toContain("harnessProfile.overrideBadge");
    const reset = container.querySelector(".harness-field-reset")! as HTMLButtonElement;
    expect(reset.disabled).toBe(false);
    fireEvent.click(reset);
    expect(onReset).toHaveBeenCalledOnce();
  });

  it("disables the reset button when disabled", () => {
    const { container } = render(<StateBadge overridden={true} onReset={vi.fn()} disabled={true} />);
    expect((container.querySelector(".harness-field-reset") as HTMLButtonElement).disabled).toBe(true);
  });
});

// --- HarnessGroup: plain toggle rows -----------------------------------------

describe("HarnessGroup — toggle rows", () => {
  it("renders the group title, hint for a seeded group, and a toggle row", () => {
    const g = group("permissions", [toggleItem("Read", { description: "d", description_path: "/r" })]);
    const { container } = renderGroup(g);
    expect(container.textContent).toContain("harnessProfile.group.permissions");
    expect(container.querySelector(".harness-group-hint")?.textContent).toBe("Perms hint");
    expect(container.querySelector(".harness-item-label")?.textContent).toBe("Read");
  });

  it("omits the hint paragraph for an unseeded group", () => {
    const g = group("other", [toggleItem("X")]);
    const { container } = renderGroup(g);
    expect(container.querySelector(".harness-group-hint")).toBeNull();
  });

  it("writes immediately for a profile-scoped toggle (no confirm)", () => {
    const g = group("other", [toggleItem("X")]);
    const { onWrite, container } = renderGroup(g, { extensionId: null });
    const cb = container.querySelector('input[type="checkbox"]')!;
    fireEvent.click(cb);
    expect(onWrite).toHaveBeenCalledWith({ path: ["other", "X"], value: true });
  });

  it("uses the extension-scoped path when extensionId is set", () => {
    const profile = emptyProfile();
    extInstance(profile, "ext1");
    const g = group("skills", [toggleItem("S")]);
    const { onWrite, container } = renderGroup(g, { extensionId: "ext1", profile });
    fireEvent.click(container.querySelector('input[type="checkbox"]')!);
    expect(onWrite).toHaveBeenCalledWith({ path: ["extension_instances", "ext1", "skills", "S"], value: true });
  });

  it("shows the overridden StateBadge and writes default back on reset", () => {
    const profile = emptyProfile();
    topLevelOverride(profile, "other", "X");
    const g = group("other", [toggleItem("X", { default_enabled: true })]);
    const { onWrite, container } = renderGroup(g, { profile });
    expect(container.querySelector(".is-overridden")).toBeTruthy();
    fireEvent.click(container.querySelector(".harness-field-reset")!);
    expect(onWrite).toHaveBeenCalledWith({ path: ["other", "X"], value: true });
  });

  it("shows the GlobalBadge (no StateBadge) for a global-scope group", () => {
    const g = group("globalthing", [toggleItem("G")], { scope: "global" });
    const { container } = renderGroup(g, { isDefault: true });
    expect(container.querySelector(".harness-field-badge.global")).toBeTruthy();
    expect(container.querySelector(".harness-field-reset")).toBeNull();
    // Title also carries the global badge.
    expect(container.querySelectorAll(".harness-field-badge.global").length).toBe(2);
  });

  it("requires confirm before applying a global toggle on a named profile, then applies", () => {
    const g = group("globalthing", [toggleItem("G")], { scope: "global" });
    const { onWrite, container } = renderGroup(g, { isDefault: false });
    fireEvent.click(container.querySelector('input[type="checkbox"]')!);
    const confirm = container.querySelector(".harness-global-confirm")!;
    expect(confirm).toBeTruthy();
    fireEvent.click(container.querySelector(".harness-global-confirm .btn-secondary")!);
    expect(onWrite).toHaveBeenCalledWith({ path: ["globalthing", "G"], value: true });
    expect(container.querySelector(".harness-global-confirm")).toBeNull();
  });

  it("cancels a pending global confirm without writing", () => {
    const g = group("globalthing", [toggleItem("G")], { scope: "global" });
    const { onWrite, container } = renderGroup(g, { isDefault: false });
    fireEvent.click(container.querySelector('input[type="checkbox"]')!);
    const buttons = container.querySelectorAll(".harness-global-confirm .btn-secondary");
    fireEvent.click(buttons[1]); // cancel
    expect(onWrite).not.toHaveBeenCalled();
    expect(container.querySelector(".harness-global-confirm")).toBeNull();
  });

  it("applies a global toggle immediately on the Default profile (no confirm)", () => {
    const g = group("globalthing", [toggleItem("G")], { scope: "global" });
    const { onWrite, container } = renderGroup(g, { isDefault: true });
    fireEvent.click(container.querySelector('input[type="checkbox"]')!);
    expect(container.querySelector(".harness-global-confirm")).toBeNull();
    expect(onWrite).toHaveBeenCalledWith({ path: ["globalthing", "G"], value: true });
  });

  it("disables the checkbox and shows locked-by holders when locked_by is set", () => {
    const g = group("other", [toggleItem("X", { locked_by: ["policy"] })]);
    const { container } = renderGroup(g);
    const cb = container.querySelector('input[type="checkbox"]')! as HTMLInputElement;
    expect(cb.disabled).toBe(true);
    expect(container.textContent).toContain("harnessProfile.lockedBy");
  });

  it("disables the checkbox when the disabled prop is set", () => {
    const g = group("other", [toggleItem("X")]);
    const { container } = renderGroup(g, { disabled: true });
    expect((container.querySelector('input[type="checkbox"]') as HTMLInputElement).disabled).toBe(true);
  });

  it("renders item detail and provider scope when present", () => {
    const g = group("other", [toggleItem("X", { detail: "chip", providers: ["claude", "codex"] })]);
    const { container } = renderGroup(g);
    expect(container.querySelector(".harness-item-detail")?.textContent).toBe("chip");
    expect(container.textContent).toContain("harnessProfile.providerScope");
  });

  it("renders an item with no description (caption stays empty, no hint link) on a hinted item group", () => {
    // permissions → itemHintKey is non-null, exercising the `hintKey ?` truthy
    // arm of the caption ternary; with no description the caption resolves to
    // "" and DescriptionLink renders nothing.
    const g = group("permissions", [toggleItem("Plain")]);
    const { container } = renderGroup(g);
    expect(container.querySelector(".harness-item-label")?.textContent).toBe("Plain");
    expect(container.querySelector(".harness-item-hint")).toBeNull();
  });

  it("renders a ui_surfaces item using the i18n label+hint keys", () => {
    const g = group("ui_surfaces", [toggleItem("statusBar")]);
    const { container } = renderGroup(g);
    expect(container.querySelector(".harness-item-label")?.textContent).toBe("statusBar");
  });

  it("diffOnly renders only overridden items", () => {
    const profile = emptyProfile();
    topLevelOverride(profile, "other", "Over");
    const g = group("other", [toggleItem("Over"), toggleItem("Plain")]);
    const { container } = renderGroup(g, { profile, diffOnly: true });
    const labels = Array.from(container.querySelectorAll(".harness-item-label")).map((e) => e.textContent);
    expect(labels).toContain("Over");
    expect(labels).not.toContain("Plain");
  });

  it("diffOnly renders nothing when no items are overridden", () => {
    const g = group("other", [toggleItem("Plain")]);
    const { container } = renderGroup(g, { diffOnly: true });
    expect(container.querySelector(".harness-group")).toBeNull();
  });
});

// --- HarnessGroup: settings rows ---------------------------------------------

function settingsGroup(items: HarnessDescriptorItem[]) {
  return group(GROUP_SETTINGS, items, { control: "settings" });
}

describe("HarnessGroup — settings rows", () => {
  it("renders nothing when there are no visible settings", () => {
    const g = settingsGroup([]);
    const { container } = renderGroup(g, { extensionId: "ext1" });
    expect(container.querySelector(".harness-group")).toBeNull();
  });

  it("secret field: writes on blur and clears the input", () => {
    const profile = emptyProfile();
    extInstance(profile, "ext1");
    const g = settingsGroup([toggleItem("apiKey", { secret: true, secret_present: false })]);
    const { onWrite, container } = renderGroup(g, { extensionId: "ext1", profile });
    const input = container.querySelector('input[type="password"]')! as HTMLInputElement;
    expect(input.placeholder).toBe("harnessProfile.secretEmpty");
    fireEvent.change(input, { target: { value: "v1" } });
    fireEvent.blur(input);
    expect(onWrite).toHaveBeenCalledWith({ path: ["extension_instances", "ext1", GROUP_SETTINGS, "apiKey"], value: "v1" });
    expect(input.value).toBe("");
  });

  it("secret field: shows the stored placeholder and does not write on empty blur", () => {
    const profile = emptyProfile();
    extInstance(profile, "ext1");
    const g = settingsGroup([toggleItem("apiKey", { secret: true, secret_present: true })]);
    const { onWrite, container } = renderGroup(g, { extensionId: "ext1", profile });
    const input = container.querySelector('input[type="password"]')! as HTMLInputElement;
    expect(input.placeholder).toBe("harnessProfile.secretStored");
    fireEvent.blur(input);
    expect(onWrite).not.toHaveBeenCalled();
  });

  it("boolean setting: writes the checked value", () => {
    const profile = emptyProfile();
    extInstance(profile, "ext1");
    const g = settingsGroup([toggleItem("flag", { type: "boolean", default_value: false })]);
    const { onWrite, container } = renderGroup(g, { extensionId: "ext1", profile });
    fireEvent.click(container.querySelector('input[type="checkbox"]')!);
    expect(onWrite).toHaveBeenCalledWith({ path: ["extension_instances", "ext1", GROUP_SETTINGS, "flag"], value: true });
  });

  it("enum setting: writes the selected option", () => {
    const profile = emptyProfile();
    extInstance(profile, "ext1");
    const g = settingsGroup([toggleItem("mode", { enum: ["a", "b"], default_value: "a" })]);
    const { onWrite, container } = renderGroup(g, { extensionId: "ext1", profile });
    const select = container.querySelector("select")! as HTMLSelectElement;
    expect(select.value).toBe("a");
    fireEvent.change(select, { target: { value: "b" } });
    expect(onWrite).toHaveBeenCalledWith({ path: ["extension_instances", "ext1", GROUP_SETTINGS, "mode"], value: "b" });
  });

  it("enum setting: resolves to an empty selection when no value is present", () => {
    const profile = emptyProfile();
    extInstance(profile, "ext1");
    const g = settingsGroup([toggleItem("mode", { enum: ["a", "b"] })]);
    const { container } = renderGroup(g, { extensionId: "ext1", profile });
    // `state.effective ?? ""` evaluates the "" arm (no default, no override);
    // happy-dom then surfaces the first option as the DOM selection.
    const select = container.querySelector("select") as HTMLSelectElement;
    expect(select.options.length).toBe(2);
    expect(select.value).toBe("a");
  });

  it("number setting: writes a Number on change and ignores unchanged blur", () => {
    const profile = emptyProfile();
    extInstance(profile, "ext1");
    const g = settingsGroup([toggleItem("count", { type: "number", default_value: 3 })]);
    const { onWrite, container } = renderGroup(g, { extensionId: "ext1", profile });
    const input = container.querySelector('input[type="number"]')! as HTMLInputElement;
    expect(input.value).toBe("3");
    fireEvent.blur(input); // unchanged
    expect(onWrite).not.toHaveBeenCalled();
    fireEvent.change(input, { target: { value: "5" } });
    fireEvent.blur(input);
    expect(onWrite).toHaveBeenCalledWith({ path: ["extension_instances", "ext1", GROUP_SETTINGS, "count"], value: 5 });
  });

  it("text setting: writes a string on change", () => {
    const profile = emptyProfile();
    const inst = extInstance(profile, "ext1");
    inst.setting_overlays = {
      name: { resolved: { value: "old", schema_hash: "" }, override: { value: "old", schema_hash: "" } },
    };
    const g = settingsGroup([toggleItem("name", { type: "string" })]);
    const { onWrite, container } = renderGroup(g, { extensionId: "ext1", profile });
    expect(container.querySelector(".is-overridden")).toBeTruthy();
    const input = container.querySelector('input[type="text"]')! as HTMLInputElement;
    expect(input.value).toBe("old");
    fireEvent.change(input, { target: { value: "new" } });
    fireEvent.blur(input);
    expect(onWrite).toHaveBeenCalledWith({ path: ["extension_instances", "ext1", GROUP_SETTINGS, "name"], value: "new" });
  });

  it("setting reset writes a clear", () => {
    const profile = emptyProfile();
    const inst = extInstance(profile, "ext1");
    inst.setting_overlays = {
      name: { resolved: { value: "x", schema_hash: "" }, override: { value: "x", schema_hash: "" } },
    };
    const g = settingsGroup([toggleItem("name", { type: "string" })]);
    const { onWrite, container } = renderGroup(g, { extensionId: "ext1", profile });
    fireEvent.click(container.querySelector(".harness-field-reset")!);
    expect(onWrite).toHaveBeenCalledWith({ path: ["extension_instances", "ext1", GROUP_SETTINGS, "name"], clear: true });
  });

  it("text setting shows empty when the resolved value is null", () => {
    const profile = emptyProfile();
    const inst = extInstance(profile, "ext1");
    inst.setting_overlays = {
      name: { resolved: null, override: { value: "x", schema_hash: "" } },
    };
    const g = settingsGroup([toggleItem("name", { type: "string" })]);
    const { container } = renderGroup(g, { extensionId: "ext1", profile });
    expect((container.querySelector('input[type="text"]') as HTMLInputElement).value).toBe("");
  });
});

// --- HarnessGroup: user instructions -----------------------------------------

describe("HarnessGroup — user instructions", () => {
  function userInstructionsProfile(content: string, overridden: boolean) {
    const profile = emptyProfile();
    (profile.fields as Record<string, unknown>).instruction_sources = {
      "ext1 user instructions": {
        resolved: { kind: "text", content },
        override: overridden ? { kind: "text", content } : null,
      },
    };
    return profile;
  }

  it("renders the textarea with the resolved content and writes on change", () => {
    const profile = userInstructionsProfile("hello", true);
    const g = group(GROUP_USER_INSTRUCTIONS, [], { control: "instructions" });
    const { onWrite, container } = renderGroup(g, { extensionId: "ext1", profile });
    const ta = container.querySelector("textarea")! as HTMLTextAreaElement;
    expect(ta.value).toBe("hello");
    fireEvent.change(ta, { target: { value: "world" } });
    fireEvent.blur(ta);
    expect(onWrite).toHaveBeenCalledWith({ path: ["extension_instances", "ext1", GROUP_USER_INSTRUCTIONS], value: "world" });
  });

  it("reset writes a clear", () => {
    const profile = userInstructionsProfile("hello", true);
    const g = group(GROUP_USER_INSTRUCTIONS, [], { control: "instructions" });
    const { onWrite, container } = renderGroup(g, { extensionId: "ext1", profile });
    fireEvent.click(container.querySelector(".harness-field-reset")!);
    expect(onWrite).toHaveBeenCalledWith({ path: ["extension_instances", "ext1", GROUP_USER_INSTRUCTIONS], clear: true });
  });

  it("diffOnly hides user instructions when not overridden", () => {
    const profile = userInstructionsProfile("hello", false);
    const g = group(GROUP_USER_INSTRUCTIONS, [], { control: "instructions" });
    const { container } = renderGroup(g, { extensionId: "ext1", profile, diffOnly: true });
    expect(container.querySelector(".harness-group")).toBeNull();
  });

  it("diffOnly shows user instructions when overridden", () => {
    const profile = userInstructionsProfile("hello", true);
    const g = group(GROUP_USER_INSTRUCTIONS, [], { control: "instructions" });
    const { container } = renderGroup(g, { extensionId: "ext1", profile, diffOnly: true });
    expect(container.querySelector("textarea")).toBeTruthy();
  });

  it("ignores an unchanged blur (no write)", () => {
    const profile = userInstructionsProfile("hello", true);
    const g = group(GROUP_USER_INSTRUCTIONS, [], { control: "instructions" });
    const { onWrite, container } = renderGroup(g, { extensionId: "ext1", profile });
    fireEvent.blur(container.querySelector("textarea")!);
    expect(onWrite).not.toHaveBeenCalled();
  });

  it("renders inherited user instructions (no reset, no overridden class) when not overridden", () => {
    const profile = userInstructionsProfile("hello", false);
    const g = group(GROUP_USER_INSTRUCTIONS, [], { control: "instructions" });
    const { container } = renderGroup(g, { extensionId: "ext1", profile });
    expect(container.querySelector(".is-overridden")).toBeNull();
    expect(container.querySelector(".harness-field-reset")).toBeNull();
    expect((container.querySelector("textarea") as HTMLTextAreaElement).value).toBe("hello");
  });
});

// --- HarnessGroup: extension-level instructions ------------------------------

describe("HarnessGroup — extension-level instructions", () => {
  function instrGroup(items: HarnessDescriptorItem[], value: unknown = true) {
    return group(GROUP_INSTRUCTIONS, items, {
      control: "instructions",
      default_granularity: "extension",
      value,
    });
  }

  it("diffOnly hides the extension-level instructions block", () => {
    const g = instrGroup([toggleItem("A")]);
    const { container } = renderGroup(g, { extensionId: "ext1", isDefault: true, diffOnly: true });
    expect(container.querySelector(".harness-group")).toBeNull();
  });

  it("renders the master toggle and each section, and writes the master path", () => {
    const profile = emptyProfile();
    extInstance(profile, "ext1");
    const g = instrGroup(
      [
        toggleItem("A", { detail: "da", providers: ["claude"], description: "desc", description_path: "/a" }),
        toggleItem("B"),
      ],
      false,
    );
    const { onWrite, onOpenDescription, container } = renderGroup(g, {
      extensionId: "ext1",
      isDefault: true,
      profile,
    });
    const cb = container.querySelector('input[type="checkbox"]')! as HTMLInputElement;
    expect(cb.checked).toBe(false);
    fireEvent.click(cb);
    expect(onWrite).toHaveBeenCalledWith({
      path: ["extension_instances", "ext1", GROUP_INSTRUCTIONS, ""],
      value: true,
    });
    // Section rows render detail + provider scope + description link.
    expect(container.textContent).toContain("da");
    expect(container.textContent).toContain("harnessProfile.providerScope");
    // Description link opens.
    const descBtn = container.querySelector("button.harness-description-link")!;
    fireEvent.click(descBtn);
    expect(onOpenDescription).toHaveBeenCalledWith("/a", "A");
  });

  it("writes the master toggle with an empty extension segment when extensionId is null", () => {
    const g = instrGroup([toggleItem("A")], false);
    const { onWrite, container } = renderGroup(g, { extensionId: null, isDefault: true });
    fireEvent.click(container.querySelector('input[type="checkbox"]')!);
    expect(onWrite).toHaveBeenCalledWith({
      path: ["extension_instances", "", GROUP_INSTRUCTIONS, ""],
      value: true,
    });
  });
});

// --- ExtensionEnabledToggle --------------------------------------------------

describe("ExtensionEnabledToggle", () => {
  function enabledGroup(items: HarnessDescriptorItem[]) {
    return group("disabled_builtin_extensions", items);
  }

  it("renders nothing when the extension item is absent", () => {
    const g = enabledGroup([toggleItem("other")]);
    const { container } = render(
      <ExtensionEnabledToggle
        group={g}
        profile={emptyProfile()}
        extensionId="ext1"
        isDefault={false}
        disabled={false}
        diffOnly={false}
        onWrite={vi.fn()}
        onOpenDescription={vi.fn()}
      />,
    );
    expect(container.querySelector(".harness-extension-enabled")).toBeNull();
  });

  it("diffOnly hides a non-overridden extension toggle", () => {
    const g = enabledGroup([toggleItem("ext1")]);
    const { container } = render(
      <ExtensionEnabledToggle
        group={g}
        profile={emptyProfile()}
        extensionId="ext1"
        isDefault={false}
        disabled={false}
        diffOnly={true}
        onWrite={vi.fn()}
        onOpenDescription={vi.fn()}
      />,
    );
    expect(container.querySelector(".harness-extension-enabled")).toBeNull();
  });

  it("renders the toggle with the labelOverride and writes to the top-level path", () => {
    const profile = emptyProfile();
    topLevelOverride(profile, "disabled_builtin_extensions", "ext1");
    const g = enabledGroup([toggleItem("ext1")]);
    const onWrite = vi.fn();
    const { container } = render(
      <ExtensionEnabledToggle
        group={g}
        profile={profile}
        extensionId="ext1"
        isDefault={false}
        disabled={false}
        diffOnly={false}
        onWrite={onWrite}
        onOpenDescription={vi.fn()}
      />,
    );
    expect(container.textContent).toContain("harnessProfile.extensionEnabled");
    // Inverted group: item present in resolved → effective off; toggling writes.
    fireEvent.click(container.querySelector('input[type="checkbox"]')!);
    expect(onWrite).toHaveBeenCalledWith({
      path: ["disabled_builtin_extensions", "ext1"],
      value: true,
    });
  });
});

// --- ExtensionGroups ---------------------------------------------------------

describe("ExtensionGroups", () => {
  function enabledGroup(enabled: boolean) {
    // Top-level profile-scope group; item present in resolved means effective on
    // for a non-inverted group.
    const profile = emptyProfile();
    (profile.fields as Record<string, unknown>).enabled_switch = {
      resolved: enabled ? ["ext1"] : [],
      override: enabled ? { add: ["ext1"], remove: [] } : null,
    };
    return { profile, enabledGroup: group("enabled_switch", [toggleItem("ext1")]) };
  }

  function makeExtension(groups: HarnessDescriptorGroup[]): HarnessDescriptorExtension {
    return {
      id: "ext1",
      name: "Ext",
      description: "",
      required: false,
      enabled: true,
      runtime_ready: true,
      runtime_not_ready_reason: "",
      groups,
    };
  }

  it("renders groups enabled when the master toggle is on", () => {
    const { profile, enabledGroup: eg } = enabledGroup(true);
    const extension = makeExtension([group("skills", [toggleItem("S")])]);
    const { container } = render(
      <ExtensionGroups
        extension={extension}
        enabledGroup={eg}
        profile={profile}
        isDefault={false}
        disabled={false}
        diffOnly={false}
        onWrite={vi.fn()}
        onOpenDescription={vi.fn()}
      />,
    );
    expect(container.querySelector(".is-extension-off")).toBeNull();
    expect(container.textContent).not.toContain("harnessProfile.extensionOffNote");
    expect(container.querySelector('[aria-disabled="false"]')).toBeTruthy();
  });

  it("renders the off-note, aria-disabled, and disables child controls when off", () => {
    const { profile, enabledGroup: eg } = enabledGroup(false);
    const extension = makeExtension([group("skills", [toggleItem("S")])]);
    const { container } = render(
      <ExtensionGroups
        extension={extension}
        enabledGroup={eg}
        profile={profile}
        isDefault={false}
        disabled={false}
        diffOnly={false}
        onWrite={vi.fn()}
        onOpenDescription={vi.fn()}
      />,
    );
    expect(container.querySelector(".is-extension-off")).toBeTruthy();
    expect(container.textContent).toContain("harnessProfile.extensionOffNote");
    expect(container.querySelector('[aria-disabled="true"]')).toBeTruthy();
    expect((container.querySelector('input[type="checkbox"]') as HTMLInputElement).disabled).toBe(true);
  });

  it("defaults to enabled when the master item is absent", () => {
    const eg = group("enabled_switch", []); // item not found
    const extension = makeExtension([group("skills", [toggleItem("S")])]);
    const { container } = render(
      <ExtensionGroups
        extension={extension}
        enabledGroup={eg}
        profile={emptyProfile()}
        isDefault={false}
        disabled={false}
        diffOnly={false}
        onWrite={vi.fn()}
        onOpenDescription={vi.fn()}
      />,
    );
    expect(container.querySelector(".is-extension-off")).toBeNull();
  });
});
