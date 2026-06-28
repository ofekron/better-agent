import type { Permission } from "../types";

// Per-kind "full bypass" target. A session whose effective permission matches
// every axis here runs with no approvals/sandbox — the behavior we warn about
// once on first send.
const BYPASS_TARGET: Record<string, Permission> = {
  claude: { mode: "bypassPermissions" },
  codex: { approval: "never", sandbox: "danger-full-access" },
  gemini: { mode: "yolo" },
  openai: { mode: "bypassPermissions" },
};

/** Resolve the axes this kind exposes (empty for kinds without permission). */
function axesForKind(kind: string): string[] {
  return Object.keys(BYPASS_TARGET[kind] ?? {});
}

/** Effective per-axis permission: session override → provider default. */
export function effectivePermission(
  kind: string,
  session: Permission | undefined,
  fallback: Permission | undefined,
): Permission {
  const out: Permission = {};
  for (const axis of axesForKind(kind)) {
    out[axis] = session?.[axis] || fallback?.[axis] || "";
  }
  return out;
}

/** True when every axis equals the kind's full-bypass target. */
export function isBypassPermission(kind: string, perm: Permission): boolean {
  const target = BYPASS_TARGET[kind];
  if (!target) return false;
  return Object.keys(target).every((axis) => perm[axis] === target[axis]);
}

/** Combined check for a session: kind + override + provider default. */
export function sessionIsBypass(
  kind: string,
  session: Permission | undefined,
  fallback: Permission | undefined,
): boolean {
  if (!BYPASS_TARGET[kind]) return false;
  return isBypassPermission(kind, effectivePermission(kind, session, fallback));
}
