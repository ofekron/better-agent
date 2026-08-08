import { useCallback, useEffect, useRef, useState } from "react";
import { SurfaceClient } from "../adapter/client";
import type { FormFieldWire, InstallableDescriptorWire } from "../adapter/wire";
import type { Template } from "../components/ProviderForm";

// D2 (ADR 0007, Package D): the backend-owned installable-provider-kind
// catalog (`GET /api/v2/surface/providers/installable`), replacing the
// frontend's static `TEMPLATES` array + `providerFormShape.ts`'s
// per-kind mode-restriction table — both deleted (backend/adapters/
// provider_adapter.py's `_TEMPLATES`/`_RESTRICTED_TEMPLATE_MODES` are now
// the one source of truth for which fields a kind's form shows and which
// auth modes it offers). REST-only: `installable_catalog()` is a static,
// code-defined table (`provider_adapter.py`'s own docstring), and
// `InstallableCatalogChanged` — the frame that WOULD signal a live change
// — is never actually broadcast anywhere, so there is nothing to
// subscribe to; fetch-once-on-mount is the correct, complete behavior,
// not a partial implementation.
//
// `Template.label`/`.blurb` here are the raw backend `display.label` /
// empty string respectively (this hook does no i18n resolution, and
// intentionally isn't a `useTranslation()`-dependent hook — a language
// switch must not need a network refetch to show new copy). Per-kind
// translated label/blurb text (the backend descriptor carries no blurb
// sentence at all — `Display` is `{label, icon_id, config_copy_key}`)
// is resolved at RENDER time by `ProviderForm.tsx`'s `TemplateGrid`,
// mirroring exactly how the deleted static `TEMPLATES`/`TEMPLATE_KEYS`
// split already worked (`Template.label`/`.blurb` were themselves never
// read for display even then — `TemplateGrid` always rendered via a
// separate id -> i18n-key lookup). Every field that determines FORM
// BEHAVIOR (which fields show, default values, allowed auth modes) comes
// from the descriptor alone; an unmapped kind still renders correctly via
// `TemplateGrid`'s fallback to this raw label — never a crash, never
// guessed business logic.

function str(v: unknown, fallback = ""): string {
  return typeof v === "string" ? v : fallback;
}

/** Maps one backend `InstallableDescriptorWire` onto the app's existing
 * `Template` shape (id/label/blurb/defaults/formSchema) so
 * `ProviderForm`/`TemplateGrid`/`RuntimeProfileWizard` keep the same
 * consumption shape they always have — only WHERE the data comes from
 * changed (fetched descriptor instead of a hardcoded array). */
export function templateFromDescriptor(descriptor: InstallableDescriptorWire): Template {
  const d = descriptor.defaults;
  return {
    id: descriptor.kind,
    label: descriptor.display.label,
    blurb: "",
    defaults: {
      name: str(d.name),
      kind: str(d.kind),
      mode: str(d.mode, "subscription") as Template["defaults"]["mode"],
      base_url: str(d.base_url),
      config_dir: str(d.config_dir),
      default_model: str(d.default_model),
      default_reasoning_effort: str(d.default_reasoning_effort) as Template["defaults"]["default_reasoning_effort"],
      ...(typeof d.api_key === "string" && d.api_key ? { api_key: d.api_key } : {}),
      ...(d.suspended === true ? { suspended: true } : {}),
    },
    formSchema: descriptor.form_schema,
  };
}

/** Any installable descriptor whose underlying `kind` matches — every
 * template sharing an underlying provider kind has a byte-identical
 * `form_schema` (`provider_adapter.py`'s `_form_schema_for` derives field
 * presence from `_template_modes(underlying_kind)` alone, never the
 * template id), so any match is a correct, non-fabricated source for an
 * EXISTING provider being edited (which has a `kind`, not a template id —
 * the two are not 1:1: e.g. "ollama"/"zai"/"custom" all create
 * `kind="claude"` records). Empty array (never `undefined`) when no
 * installed template shares that kind — callers render every optional
 * section (mode toggle, api key, config dir) rather than guessing. */
export function formSchemaForProviderKind(
  templates: readonly Template[],
  kind: string,
): FormFieldWire[] {
  return templates.find((t) => t.defaults.kind === kind)?.formSchema ?? [];
}

export interface InstallableProvidersState {
  templates: Template[];
  loading: boolean;
  error: string | null;
  /** Re-runs the fetch (e.g. a user-triggered retry after a failed load).
   * Safe to call while already loading — the newest call wins, superseding
   * any in-flight response (same `loadSeq` guard `useRuntimeProfiles.ts`
   * uses for its own refresh()). */
  retry: () => void;
}

const client = new SurfaceClient();

/** A9: consumers (`SettingsPage.tsx`'s `TemplateGrid`/`EditProvider`,
 * `RuntimeProfileWizard.tsx`) must be able to render an explicit
 * loading/error state — and never a wrong-kind label derived from an
 * empty in-flight/failed `templates` array — so this hook exposes
 * `loading`/`error`/`retry` for them to gate on, rather than leaving them
 * to infer state from `templates.length === 0` (which is ALSO the
 * legitimate steady-state for "no installed template shares this kind",
 * see `formSchemaForProviderKind`'s own docstring). */
export function useInstallableProviders(): InstallableProvidersState {
  const [templates, setTemplates] = useState<Template[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const loadSeq = useRef(0);

  // No synchronous `setLoading(true)` here — `loading` already starts
  // `true` (its `useState(true)` default) for the effect-driven initial
  // call below, and `retry()` (never invoked from inside an effect body)
  // sets it explicitly. Keeps this function callable straight from a
  // `useEffect` body without a synchronous-setState-in-effect lint issue.
  const load = useCallback(() => {
    const seq = ++loadSeq.current;
    client
      .fetchInstallableProviders()
      .then((envelope) => {
        if (loadSeq.current !== seq) return;
        setTemplates(envelope.value.map(templateFromDescriptor));
        setError(null);
      })
      .catch((e) => {
        if (loadSeq.current !== seq) return;
        setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (loadSeq.current === seq) setLoading(false);
      });
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const retry = useCallback(() => {
    setLoading(true);
    setError(null);
    load();
  }, [load]);

  return { templates, loading, error, retry };
}
