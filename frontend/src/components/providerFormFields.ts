// Pure, no-React helpers over `InstallableDescriptor.form_schema`
// (ADR 0007, Package D) — kept in their own module (not ProviderForm.tsx)
// for the same reason the deleted `providerFormShape.ts` was: React Fast
// Refresh only works when a file exports components alone
// (`react-refresh/only-export-components`), and these are genuinely
// testable in isolation (see tests/providerFormFields.test.ts). Unlike
// the deleted module, there is no static per-kind table here — every
// answer comes from the `form_schema` array the backend descriptor
// supplies (`useInstallableProviders.ts`'s `formSchemaForProviderKind`
// resolves WHICH schema for a given kind; these functions only read it).

import type { FormFieldWire } from "../adapter/wire";
import type { Provider } from "../types";

/** `modes` a field-schema offers: the schema's `"mode"` field's `choices`
 * when present (multi-mode kind), else the single mode already baked into
 * `initial`/`defaults` (a kind with no `"mode"` field in its schema, per
 * `provider_adapter.py`'s `_form_schema_for`, offers exactly one mode).
 * Edit mode additionally preserves an already-persisted mode the current
 * schema no longer offers (matches the deleted `availableModesForForm`'s
 * "never silently rewrite" behavior) — a record saved under a mode a kind
 * used to support but no longer restricts to stays visible/selectable. */
export function modesFromSchema(
  formSchema: FormFieldWire[],
  formMode: "create" | "edit",
  initialMode: Provider["mode"],
): Provider["mode"][] {
  const modeField = formSchema.find((f) => f.name === "mode");
  const base = (modeField?.choices as Provider["mode"][] | undefined) ?? [initialMode];
  if (formMode === "edit" && !base.includes(initialMode)) return [...base, initialMode];
  return base;
}

export function schemaHasField(formSchema: FormFieldWire[], name: string): boolean {
  return formSchema.some((f) => f.name === name);
}

export function fieldLabelKey(formSchema: FormFieldWire[], name: string, fallback: string): string {
  return formSchema.find((f) => f.name === name)?.label_key ?? fallback;
}

/** Closure 4 (form-copy regression): `FormField.placeholder_key`/
 * `.hint_key` now carry the per-kind copy the deleted frontend
 * `apiEnvCopyForKind`/`configDirCopyForKind` used to compute locally —
 * `null` on the wire (no placeholder/hint for this field) falls back to
 * `fallback`, same not-fabricated-when-absent contract `fieldLabelKey`
 * already has. */
export function fieldPlaceholderKey(formSchema: FormFieldWire[], name: string, fallback: string): string {
  return formSchema.find((f) => f.name === name)?.placeholder_key ?? fallback;
}

export function fieldHintKey(formSchema: FormFieldWire[], name: string, fallback: string | null): string | null {
  return formSchema.find((f) => f.name === name)?.hint_key ?? fallback;
}
