import { describe, expect, it } from "vitest";
import type {
  HarnessDescriptorGroup,
  HarnessDescriptorItem,
  HarnessProfile,
  HarnessProfileExtensionInstanceView,
  HarnessProfileFieldView,
  HarnessProfileListFieldView,
} from "../src/types";
import {
  clearAllWrites,
  groupOverrideCount,
  isItemOverridden,
  settingState,
  toggleState,
  userInstructionSourceName,
  userInstructionsState,
} from "../src/components/harness/resolve";
import {
  GROUP_DISABLED_BUILTIN_TOOLS,
  GROUP_SETTINGS,
  GROUP_USER_INSTRUCTIONS,
  SCOPE_GLOBAL,
  SCOPE_PROFILE,
} from "../src/components/harness/types";

// ---- fixtures ------------------------------------------------------------

function item(name: string, over: Partial<HarnessDescriptorItem> = {}): HarnessDescriptorItem {
  return { name, label: name, description: "", ...over };
}

function group(
  id: string,
  items: HarnessDescriptorItem[],
  over: Partial<HarnessDescriptorGroup> = {},
): HarnessDescriptorGroup {
  return { id, scope: SCOPE_PROFILE, control: "item_toggles", items, value: null, ...over };
}

function makeProfile(fields: Partial<HarnessProfile["fields"]>): HarnessProfile {
  return {
    id: "p",
    name: "P",
    description: "",
    revision: "r",
    created_at: "",
    updated_at: "",
    fields: {
      extension_instances: {},
      disabled_builtin_tools: { resolved: [], override: null },
      disabled_builtin_extensions: { resolved: [], override: null },
      disabled_runtime_skills: { resolved: [], override: null },
      instruction_sources: {},
      ...fields,
    } as HarnessProfile["fields"],
    mcp_overrides: {},
    skill_overrides: {},
    native_harness_overrides: {},
    provider_run_config_overlay: {},
  };
}

function listView(
  resolved: string[],
  override: HarnessProfileListFieldView["override"] = null,
): HarnessProfileListFieldView {
  return { resolved, override };
}

function extProfile(
  extId: string,
  instance: Partial<HarnessProfileExtensionInstanceView>,
): HarnessProfile {
  const p = makeProfile({});
  p.fields.extension_instances = {
    [extId]: {
      mcp_servers: listView([]),
      skills: listView([]),
      instruction_names: listView([]),
      setting_overlays: {},
      headless: { resolved: false, override: null },
      ...instance,
    },
  };
  return p;
}

// ---- userInstructionSourceName -------------------------------------------

describe("userInstructionSourceName", () => {
  it("formats the instruction-source key for an extension", () => {
    expect(userInstructionSourceName("my.ext")).toBe("my.ext user instructions");
  });
});

// ---- toggleState ---------------------------------------------------------

describe("toggleState", () => {
  it("returns the live default for a global field (never overridable)", () => {
    const g = group("anything", [item("a")], { scope: SCOPE_GLOBAL });
    g.items[0].default_enabled = true;
    expect(toggleState(makeProfile({}), g, g.items[0], null)).toEqual({
      effective: true,
      overridden: false,
    });
  });

  it("falls back to default when the profile has no view for the group", () => {
    const g = group("skills", [item("a")]);
    g.items[0].default_enabled = false;
    expect(toggleState(makeProfile({}), g, g.items[0], null)).toEqual({
      effective: false,
      overridden: false,
    });
  });

  it("falls back when an extension instance does not exist", () => {
    const g = group("skills", [item("a")]);
    g.items[0].default_enabled = true;
    expect(toggleState(makeProfile({}), g, g.items[0], "missing.ext")).toEqual({
      effective: true,
      overridden: false,
    });
  });

  it("reads membership from a top-level list field", () => {
    const g = group("disabled_builtin_tools", [item("a"), item("b")]);
    const profile = makeProfile({
      disabled_builtin_tools: listView(["a"], null),
    });
    // inverted group: present means off
    expect(toggleState(profile, g, g.items[0], null)).toEqual({ effective: false, overridden: false });
    expect(toggleState(profile, g, g.items[1], null)).toEqual({ effective: true, overridden: false });
  });

  it("reads membership from an extension instance list field", () => {
    const g = group("skills", [item("a"), item("b")]);
    const profile = extProfile("ext1", { skills: listView(["a"], null) });
    expect(toggleState(profile, g, g.items[0], "ext1")).toEqual({ effective: true, overridden: false });
    expect(toggleState(profile, g, g.items[1], "ext1")).toEqual({ effective: false, overridden: false });
  });

  it("marks overridden true when the item is in the override add list", () => {
    const g = group("skills", [item("a")]);
    const profile = makeProfile({
      disabled_builtin_tools: listView([], null), // unused
    });
    (profile.fields as Record<string, unknown>).skills = listView([], { add: ["a"], remove: [] });
    expect(toggleState(profile, g, g.items[0], null).overridden).toBe(true);
  });

  it("marks overridden true when the item is in the override remove list", () => {
    const g = group("skills", [item("a")]);
    const profile = makeProfile({});
    (profile.fields as Record<string, unknown>).skills = listView(["a"], { add: [], remove: ["a"] });
    expect(toggleState(profile, g, g.items[0], null).overridden).toBe(true);
  });

  it("marks overridden false when the item is in neither delta list", () => {
    const g = group("skills", [item("a")]);
    const profile = makeProfile({});
    (profile.fields as Record<string, unknown>).skills = listView(
      ["a"],
      { add: ["other"], remove: ["other"] },
    );
    expect(toggleState(profile, g, g.items[0], null).overridden).toBe(false);
  });
});

// ---- settingState --------------------------------------------------------

describe("settingState", () => {
  it("falls back to default_value when no overlay entry exists", () => {
    const profile = extProfile("ext1", {});
    expect(settingState(profile, "ext1", item("s", { default_value: 42 }))).toEqual({
      effective: 42,
      overridden: false,
    });
  });

  it("returns the resolved value and overridden flag when an entry exists", () => {
    const profile = extProfile("ext1", {
      setting_overlays: {
        s: { resolved: { value: "on", schema_hash: "h" }, override: { value: "on", schema_hash: "h" } },
      },
    });
    expect(settingState(profile, "ext1", item("s"))).toEqual({ effective: "on", overridden: true });
  });

  it("reports overridden false when the override is null", () => {
    const profile = extProfile("ext1", {
      setting_overlays: {
        s: { resolved: { value: 1, schema_hash: "h" }, override: null },
      },
    });
    expect(settingState(profile, "ext1", item("s"))).toEqual({ effective: 1, overridden: false });
  });

  it("reports effective undefined when resolved is null", () => {
    const profile = extProfile("ext1", {
      setting_overlays: { s: { resolved: null, override: null } },
    });
    expect(settingState(profile, "ext1", item("s"))).toEqual({ effective: undefined, overridden: false });
  });
});

// ---- userInstructionsState ----------------------------------------------

describe("userInstructionsState", () => {
  it("returns empty string when no instruction source exists", () => {
    const profile = extProfile("ext1", {});
    expect(userInstructionsState(profile, "ext1")).toEqual({ effective: "", overridden: false });
  });

  it("returns the resolved content and override flag", () => {
    const value: HarnessProfileFieldView<{ content?: string }> = {
      resolved: { content: "do the thing" },
      override: { content: "do the thing" },
    };
    const profile = extProfile("ext1", {});
    profile.fields.instruction_sources[userInstructionSourceName("ext1")] = value as never;
    expect(userInstructionsState(profile, "ext1")).toEqual({
      effective: "do the thing",
      overridden: true,
    });
  });

  it("coerces a missing/undefined content to empty string", () => {
    const profile = extProfile("ext1", {});
    profile.fields.instruction_sources[userInstructionSourceName("ext1")] = {
      resolved: {},
      override: null,
    } as never;
    expect(userInstructionsState(profile, "ext1")).toEqual({ effective: "", overridden: false });
  });

  it("coerces a null resolved to empty string", () => {
    const profile = extProfile("ext1", {});
    profile.fields.instruction_sources[userInstructionSourceName("ext1")] = {
      resolved: null,
      override: null,
    } as never;
    expect(userInstructionsState(profile, "ext1")).toEqual({ effective: "", overridden: false });
  });
});

// ---- isItemOverridden ----------------------------------------------------

describe("isItemOverridden", () => {
  it("is never overridden for a global field", () => {
    const g = group("x", [item("a")], { scope: SCOPE_GLOBAL });
    expect(isItemOverridden(makeProfile({}), g, g.items[0], null)).toBe(false);
  });

  it("routes settings through settingState for an extension, respecting secret", () => {
    const g = group(GROUP_SETTINGS, [item("s", { secret: false })]);
    const profile = extProfile("ext1", {
      setting_overlays: {
        s: { resolved: { value: 1, schema_hash: "h" }, override: { value: 1, schema_hash: "h" } },
      },
    });
    expect(isItemOverridden(profile, g, g.items[0], "ext1")).toBe(true);
    // a secret setting is not surfaced as overridable
    const gSecret = group(GROUP_SETTINGS, [item("s", { secret: true })]);
    expect(isItemOverridden(profile, gSecret, gSecret.items[0], "ext1")).toBe(false);
  });

  it("routes all other groups through toggleState.overridden", () => {
    const g = group("skills", [item("a")]);
    const profile = makeProfile({});
    (profile.fields as Record<string, unknown>).skills = listView([], { add: ["a"], remove: [] });
    expect(isItemOverridden(profile, g, g.items[0], null)).toBe(true);
  });
});

// ---- groupOverrideCount --------------------------------------------------

describe("groupOverrideCount", () => {
  it("is 0 for a global field", () => {
    const g = group("x", [item("a")], { scope: SCOPE_GLOBAL });
    expect(groupOverrideCount(makeProfile({}), g, null)).toBe(0);
  });

  it("counts a user-instructions override as 1/0 for an extension", () => {
    const g = group(GROUP_USER_INSTRUCTIONS, []);
    const profile = extProfile("ext1", {});
    expect(groupOverrideCount(profile, g, "ext1")).toBe(0);
    profile.fields.instruction_sources[userInstructionSourceName("ext1")] = {
      resolved: { content: "x" },
      override: { content: "x" },
    } as never;
    expect(groupOverrideCount(profile, g, "ext1")).toBe(1);
  });

  it("counts non-secret overridden settings for an extension", () => {
    const g = group(GROUP_SETTINGS, [
      item("a", { secret: false }),
      item("b", { secret: true }),
      item("c", { secret: false }),
    ]);
    const profile = extProfile("ext1", {
      setting_overlays: {
        a: { resolved: { value: 1, schema_hash: "h" }, override: { value: 1, schema_hash: "h" } },
        b: { resolved: { value: 2, schema_hash: "h" }, override: { value: 2, schema_hash: "h" } },
        // c has no entry → not overridden
      },
    });
    expect(groupOverrideCount(profile, g, "ext1")).toBe(1);
  });

  it("is 0 for a list group with no view at all", () => {
    const g = group("skills", [item("a")]);
    expect(groupOverrideCount(makeProfile({}), g, null)).toBe(0);
  });

  it("is 0 for a list group whose view has a null override", () => {
    const g = group("disabled_builtin_tools", [item("a")]);
    const profile = makeProfile({
      disabled_builtin_tools: listView(["a"], null),
    });
    expect(groupOverrideCount(profile, g, null)).toBe(0);
  });

  it("counts items present in a list group's override delta", () => {
    const g = group("disabled_builtin_tools", [item("a"), item("b"), item("c")]);
    const profile = makeProfile({
      disabled_builtin_tools: listView([], { add: ["a", "c"], remove: [] }),
    });
    expect(groupOverrideCount(profile, g, null)).toBe(2);
  });
});

// ---- clearAllWrites ------------------------------------------------------

describe("clearAllWrites", () => {
  it("emits nothing when no group has an override", () => {
    const g = group("skills", [item("a")]);
    expect(clearAllWrites(makeProfile({}), [{ group: g, extensionId: null }])).toEqual([]);
  });

  it("clears a top-level (non-extension) list override by group+item path", () => {
    const g = group("disabled_builtin_tools", [item("a")]);
    const profile = makeProfile({
      disabled_builtin_tools: listView([], { add: ["a"], remove: [] }),
    });
    expect(clearAllWrites(profile, [{ group: g, extensionId: null }])).toEqual([
      { path: ["disabled_builtin_tools", "a"], clear: true },
    ]);
  });

  it("uses an empty item-name segment when the group has no items", () => {
    // The only count>0 path that does not require group.items is the
    // user-instructions source, so an itemless group still clears there.
    const g = group(GROUP_USER_INSTRUCTIONS, []);
    const profile = extProfile("ext1", {});
    profile.fields.instruction_sources[userInstructionSourceName("ext1")] = {
      resolved: { content: "x" },
      override: { content: "x" },
    } as never;
    expect(clearAllWrites(profile, [{ group: g, extensionId: "ext1" }])).toEqual([
      { path: ["extension_instances", "ext1", GROUP_USER_INSTRUCTIONS, ""], clear: true },
    ]);
  });

  it("clears each overridden non-secret setting individually", () => {
    const g = group(GROUP_SETTINGS, [item("a", { secret: false }), item("b", { secret: true })]);
    const profile = extProfile("ext1", {
      setting_overlays: {
        a: { resolved: { value: 1, schema_hash: "h" }, override: { value: 1, schema_hash: "h" } },
        b: { resolved: { value: 2, schema_hash: "h" }, override: { value: 2, schema_hash: "h" } },
      },
    });
    expect(clearAllWrites(profile, [{ group: g, extensionId: "ext1" }])).toEqual([
      { path: ["extension_instances", "ext1", GROUP_SETTINGS, "a"], clear: true },
    ]);
  });

  it("clears an extension list-group override by the generic path", () => {
    const g = group("skills", [item("a")]);
    const profile = extProfile("ext1", { skills: listView([], { add: ["a"], remove: [] }) });
    expect(clearAllWrites(profile, [{ group: g, extensionId: "ext1" }])).toEqual([
      { path: ["extension_instances", "ext1", "skills", "a"], clear: true },
    ]);
  });
});
