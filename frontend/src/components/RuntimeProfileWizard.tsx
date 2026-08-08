import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import type { Provider, ProviderRunner, ReasoningEffort } from "../types";
import { API, createRuntimeProfile } from "../api";
import { useRuntimeProfiles } from "../hooks/useRuntimeProfiles";
import { useInstallableProviders } from "../hooks/useInstallableProviders";
import { useProviderModelCatalog } from "../hooks/useProviderModelCatalog";
import { useBusEffect } from "../hooks/useBusEffect";
import { EffortDefaultField, ModelDefaultField } from "./RuntimeProfileFields";
import { providerNickname } from "../utils/providerDisplayName";
import { effortsForRunner, runnerLabelKey } from "./modelPicker";
import { ProviderForm, TemplateGrid, type FormPayload } from "./ProviderForm";

// ---------------------------------------------------------------------------
// Runtime-profile creation wizard: provider (account + auth) → runner →
// defaults (model, effort when relevant). Provider auth reuses the account
// surfaces (TemplateGrid + ProviderForm + the provider OAuth login route);
// profile creation posts to /api/runtime-profiles. The installable-kind
// catalog (`useInstallableProviders`) is descriptor-driven per ADR 0007,
// Package D — see ProviderForm.tsx's module docstring.
// ---------------------------------------------------------------------------

type Step = "provider" | "runner" | "defaults";
const STEP_ORDER: Step[] = ["provider", "runner", "defaults"];

type ProviderStage =
  | { kind: "pick" }
  | { kind: "templates" }
  | { kind: "form"; templateId: string };

const WS_TOPICS = ["ws_connection_changed"] as const;

function providerDisplayName(provider: Provider): string {
  const nick = providerNickname(provider);
  return nick ? `${provider.name} — ${nick}` : provider.name;
}

export function RuntimeProfileWizard({
  providers,
  onRefetchProviders,
  onDone,
  onClose,
}: {
  providers: Provider[];
  onRefetchProviders: () => Promise<void> | void;
  /** Wizard finished (created profile id) or was abandoned (null). */
  onDone: (createdProfileId: string | null) => void;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const { profiles } = useRuntimeProfiles();
  const {
    templates,
    loading: installableLoading,
    error: installableError,
    retry: retryInstallable,
  } = useInstallableProviders();
  const [step, setStep] = useState<Step>("provider");
  const [stage, setStage] = useState<ProviderStage>({ kind: "pick" });
  const [providerId, setProviderId] = useState("");
  const [createdFromTemplate, setCreatedFromTemplate] = useState<string | null>(null);
  const [runner, setRunner] = useState<ProviderRunner | "">("");
  const [name, setName] = useState("");
  const [nameTouched, setNameTouched] = useState(false);
  const [model, setModel] = useState("");
  const [effort, setEffort] = useState<ReasoningEffort | "">("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  // Truthful offline state: profile creation needs a live backend (no
  // offline backlog for it) — reflect the WS link and gate the mutations.
  const [connected, setConnected] = useState(true);
  useBusEffect(WS_TOPICS, (payload) => {
    if (payload && typeof payload === "object" && "connected" in payload) {
      setConnected((payload as { connected?: boolean }).connected !== false);
    }
  });

  const provider = providers.find((p) => p.id === providerId);
  const runnerOptions = provider?.runner_options ?? [];
  const takenRunners = useMemo(
    () =>
      new Set(
        profiles
          .filter((p) => p.provider_id === providerId)
          .map((p) => p.runner),
      ),
    [profiles, providerId],
  );

  // Desktop-only OAuth login: the provider CLI opens the OS browser and binds
  // a localhost callback, so the browser must share the backend's machine.
  const loginEnabled =
    typeof window !== "undefined" &&
    ["localhost", "127.0.0.1", "[::1]"].includes(window.location.hostname);
  const [loginPending, setLoginPending] = useState(false);
  const [loginError, setLoginError] = useState("");
  const loginState = provider?.login_state;
  const loginStatus = loginState?.status ?? "idle";
  const authenticated = loginState?.authenticated === true;
  const loginRunning = loginStatus === "login_running" || loginPending;
  const loginSupported =
    loginEnabled &&
    !!provider &&
    provider.mode === "subscription" &&
    (provider.kind === "claude" || provider.kind === "codex");

  const runLogin = async () => {
    if (!provider) return;
    setLoginPending(true);
    setLoginError("");
    try {
      const r = await fetch(`${API}/api/providers/${provider.id}/login`, { method: "POST" });
      if (!r.ok) {
        let message = "login failed";
        try {
          const body = await r.json();
          if (body?.detail) message = String(body.detail);
        } catch {
          /* keep default */
        }
        setLoginError(message);
      }
    } finally {
      setLoginPending(false);
    }
  };
  const cancelLogin = async () => {
    if (!provider) return;
    try {
      await fetch(`${API}/api/providers/${provider.id}/login/cancel`, { method: "POST" });
    } catch {
      /* best-effort cancel */
    }
  };

  const template = createdFromTemplate
    ? templates.find((tpl) => tpl.id === createdFromTemplate) ?? null
    : null;

  const effortOptions = provider && runner ? effortsForRunner(provider, runner) : [];

  const catalogState = useProviderModelCatalog(step === "defaults" ? providerId : "");
  const { catalog } = catalogState;

  const runnerLabel = (r: ProviderRunner) =>
    provider ? t(runnerLabelKey(provider.kind, r)) : r;

  const enterDefaults = (nextRunner: ProviderRunner) => {
    if (!provider) return;
    setRunner(nextRunner);
    if (!nameTouched) {
      setName(`${provider.name} (${runnerLabel(nextRunner)})`);
    }
    const options = effortsForRunner(provider, nextRunner);
    const seedEffort = template?.defaults.default_reasoning_effort || "";
    setEffort(
      seedEffort && options.includes(seedEffort as ReasoningEffort)
        ? (seedEffort as ReasoningEffort)
        : options.includes("medium")
          ? "medium"
          : options[0] ?? "",
    );
    if (!model) setModel(template?.defaults.default_model ?? "");
    setError("");
    setStep("defaults");
  };

  // Fill the model from the catalog once it arrives, when nothing (template
  // seed or user choice) picked one yet; drop a choice the authoritative
  // catalog does not know.
  useEffect(() => {
    if (step !== "defaults" || !catalog || catalog.provider_id !== providerId) return;
    if (!model && catalog.models.length) {
      setModel(catalog.models[0]);
      return;
    }
    if (
      model
      && catalog.authoritative
      && catalog.status === "current"
      && catalog.models.length
      && !catalog.models.includes(model)
    ) {
      setModel(catalog.models[0]);
    }
  }, [catalog, model, providerId, step]);

  const selectProvider = (id: string) => {
    setProviderId(id);
    setRunner("");
    setModel("");
    setNameTouched(false);
    setError("");
  };

  const createProvider = async (payload: FormPayload, templateId: string) => {
    setError("");
    try {
      const r = await fetch(`${API}/api/providers`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!r.ok) throw new Error(await r.text());
      const record = (await r.json()) as Provider;
      await onRefetchProviders();
      selectProvider(record.id);
      setCreatedFromTemplate(templateId);
      setStage({ kind: "pick" });
    } catch (e) {
      setError(e instanceof Error ? e.message : "create failed");
      throw e;
    }
  };

  const submitProfile = async () => {
    if (!provider || !runner) return;
    setSubmitting(true);
    setError("");
    try {
      const profile = await createRuntimeProfile({
        provider_id: provider.id,
        runner,
        name: name.trim(),
        default_model: model.trim(),
        default_reasoning_effort: effort,
      });
      onDone(profile.id);
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e);
      setError(
        message.includes("already exists")
          ? t("runtimeProfile.duplicatePair")
          : message,
      );
    } finally {
      setSubmitting(false);
    }
  };

  const goBack = () => {
    setError("");
    if (step === "defaults") {
      setStep("runner");
      return;
    }
    if (step === "runner") {
      setStep("provider");
      return;
    }
    if (stage.kind === "form") {
      setStage({ kind: "templates" });
      return;
    }
    if (stage.kind === "templates") {
      setStage({ kind: "pick" });
      return;
    }
    onDone(null);
  };

  // Inline provider creation renders the full account form with its own
  // header/footer chrome; the wizard frame resumes once it submits or backs
  // out.
  if (step === "provider" && stage.kind === "form") {
    const tpl = templates.find((item) => item.id === stage.templateId);
    if (!tpl) return null; // templates fetch still in flight — TemplateGrid is the only entry point here, and it only ever offers ids from this same array.
    return (
      <ProviderForm
        mode="create"
        initial={tpl.defaults}
        initialHasKey={false}
        formSchema={tpl.formSchema}
        onClose={onClose}
        onBack={goBack}
        onSubmit={(payload) => createProvider(payload, stage.templateId)}
      />
    );
  }

  const stepIndex = STEP_ORDER.indexOf(step);

  return (
    <>
      <div className="modal-header">
        <button className="modal-back" onClick={goBack} title={t("setup.backTitle")}>
          &larr;
        </button>
        <h2>{t("runtimeProfile.wizardTitle")}</h2>
        <button className="modal-close" onClick={onClose}>
          &times;
        </button>
      </div>
      <div className="modal-body">
        <div className="rpw-steps" aria-hidden="true">
          {STEP_ORDER.map((item, index) => (
            <span
              key={item}
              className={`rpw-step-pip${index === stepIndex ? " is-active" : ""}${index < stepIndex ? " is-done" : ""}`}
            >
              {t(`runtimeProfile.step.${item}`)}
            </span>
          ))}
        </div>
        {!connected && (
          <div className="setup-error" role="alert">
            {t("runtimeProfile.offlineHint")}
          </div>
        )}
        {error && step !== "defaults" && (
          <div className="setup-error" role="alert">{error}</div>
        )}

        {step === "provider" && stage.kind === "pick" && (
          <div className="rpw-step" key="provider-pick">
            <p className="setup-mode-desc">{t("runtimeProfile.pickProvider")}</p>
            <div className="provider-templates">
              {providers.map((p) => (
                <button
                  key={p.id}
                  type="button"
                  className={`provider-template-card rpw-provider-card${p.id === providerId ? " is-selected" : ""}`}
                  disabled={p.suspended}
                  onClick={() => selectProvider(p.id)}
                >
                  <div className="provider-template-name">
                    {providerDisplayName(p)}
                    {p.suspended && (
                      <span className="provider-suspended-pill">{t("setup.suspended")}</span>
                    )}
                  </div>
                  <div className="provider-template-blurb">
                    {p.mode === "subscription" ? t("setup.subscriptionMode") : t("setup.apiKeyButton")}
                    {p.base_url ? ` · ${p.base_url}` : ""}
                  </div>
                </button>
              ))}
              <button
                type="button"
                className="provider-template-card rpw-new-provider-card"
                disabled={!connected}
                onClick={() => setStage({ kind: "templates" })}
              >
                <div className="provider-template-name">{t("runtimeProfile.newProviderCard")}</div>
                <div className="provider-template-blurb">{t("runtimeProfile.newProviderCardBlurb")}</div>
              </button>
            </div>
            {provider && loginSupported && (
              <div className="rpw-login-row">
                {authenticated && !loginRunning ? (
                  <span className="setup-field-hint">{t("runtimeProfile.signedIn")}</span>
                ) : (
                  <>
                    <button
                      type="button"
                      className={loginStatus === "login_failed" || loginError ? "btn-warning" : "btn-secondary"}
                      disabled={loginRunning || !connected}
                      onClick={() => void runLogin()}
                      title={loginError || (loginStatus === "login_failed" ? loginState?.message : undefined)}
                    >
                      {loginRunning && <span className="retrying-spinner" aria-hidden="true" />}
                      {loginRunning
                        ? t("setup.signingIn")
                        : loginStatus === "login_failed"
                          ? t("setup.retryLogin")
                          : t("setup.logIn")}
                    </button>
                    {loginRunning && (
                      <button type="button" className="btn-secondary" onClick={() => void cancelLogin()}>
                        {t("setup.cancelLogin")}
                      </button>
                    )}
                  </>
                )}
              </div>
            )}
          </div>
        )}

        {step === "provider" && stage.kind === "templates" && (
          <div className="rpw-step" key="provider-templates">
            <p className="setup-mode-desc">{t("setup.pickTemplate")}</p>
            <TemplateGrid
              templates={templates}
              loading={installableLoading}
              error={installableError}
              onRetry={retryInstallable}
              onPick={(templateId) => setStage({ kind: "form", templateId })}
            />
          </div>
        )}

        {step === "runner" && provider && (
          <div className="rpw-step" key="runner">
            <p className="setup-mode-desc">{t("runtimeProfile.pickRunner")}</p>
            <div className="provider-templates">
              {runnerOptions.map((option) => {
                const taken = takenRunners.has(option);
                return (
                  <button
                    key={option}
                    type="button"
                    data-testid={`rpw-runner-${option}`}
                    className={`provider-template-card rpw-runner-card${option === runner ? " is-selected" : ""}`}
                    disabled={taken}
                    onClick={() => enterDefaults(option)}
                  >
                    <div className="provider-template-name">{runnerLabel(option)}</div>
                    <div className="provider-template-blurb">
                      {taken ? t("runtimeProfile.runnerTaken") : t("setup.runnerHint")}
                    </div>
                  </button>
                );
              })}
            </div>
          </div>
        )}

        {step === "defaults" && provider && runner && (
          <div className="rpw-step" key="defaults">
            <p className="setup-mode-desc">{t("runtimeProfile.pickDefaults")}</p>
            <div className="setup-field">
              <label>{t("runtimeProfile.nameLabel")}</label>
              <input
                type="text"
                value={name}
                onChange={(e) => {
                  setName(e.target.value);
                  setNameTouched(true);
                }}
                spellCheck={false}
              />
            </div>
            <ModelDefaultField
              catalogState={catalogState}
              model={model}
              onModelChange={setModel}
            />
            <EffortDefaultField
              options={effortOptions}
              effort={effort}
              onEffortChange={setEffort}
            />
            {error && <div className="setup-error" role="alert">{error}</div>}
          </div>
        )}
      </div>
      <div className="modal-footer">
        <button className="setup-cancel-btn" onClick={goBack}>
          {t("setup.backTitle")}
        </button>
        {step === "provider" && (
          <button
            className="setup-save-btn"
            disabled={!provider}
            onClick={() => {
              setError("");
              setStep("runner");
            }}
          >
            {t("runtimeProfile.continue")}
          </button>
        )}
        {step === "runner" && (
          <button
            className="setup-save-btn"
            disabled={!runner}
            onClick={() => runner && enterDefaults(runner)}
          >
            {t("runtimeProfile.continue")}
          </button>
        )}
        {step === "defaults" && (
          <button
            className="setup-save-btn"
            data-testid="rpw-create-profile"
            disabled={submitting || !connected || !name.trim()}
            onClick={() => void submitProfile()}
          >
            {submitting ? t("runtimeProfile.creating") : t("runtimeProfile.createButton")}
          </button>
        )}
      </div>
    </>
  );
}
