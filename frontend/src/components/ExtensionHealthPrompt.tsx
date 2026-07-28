import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  useExtensionHealthDecision,
  type ActiveHealthDecision,
  type HealthDecisionAction,
} from "../hooks/useExtensionHealthDecision";

export type { ActiveHealthDecision, HealthDecisionAction };

interface PromptProps {
  decision: ActiveHealthDecision;
  /** Returns true once the backend has acknowledged the decision. The
   * caller (container) refetches and unmounts the prompt only after this
   * resolves true; on false the prompt stays visible with an error. */
  onSubmit: (action: HealthDecisionAction) => Promise<boolean>;
}

function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = useState(() =>
    typeof window !== "undefined" && window.matchMedia
      ? window.matchMedia("(prefers-reduced-motion: reduce)").matches
      : false,
  );
  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    const onChange = () => setReduced(mq.matches);
    mq.addEventListener?.("change", onChange);
    return () => mq.removeEventListener?.("change", onChange);
  }, []);
  return reduced;
}

export function ExtensionHealthPrompt({ decision, onSubmit }: PromptProps) {
  const { t } = useTranslation();
  const reduced = usePrefersReducedMotion();
  const [inFlightAction, setInFlightAction] = useState<HealthDecisionAction | null>(null);
  const [errored, setErrored] = useState(false);

  const cohortNames = decision.pending.cohort
    .map((member) => member.name || member.extension_id)
    .filter((name, index, arr) => arr.indexOf(name) === index);
  const primaryName = decision.extensionName || decision.extensionId;

  const submit = useCallback(
    async (action: HealthDecisionAction) => {
      setErrored(false);
      setInFlightAction(action);
      try {
        const ok = await onSubmit(action);
        if (!ok) setErrored(true);
        // On success the container refetches and unmounts this prompt; the
        // in-flight spinner is cleared only on failure so the prompt stays
        // visibly actionable until the backend ack actually lands.
        if (!ok) setInFlightAction(null);
      } catch {
        setErrored(true);
        setInFlightAction(null);
      }
    },
    [onSubmit],
  );

  return (
    <article
      className={`extension-health-prompt${reduced ? " extension-health-prompt--reduced" : ""}`}
      data-testid="extension-health-prompt"
      data-extension-id={decision.extensionId}
      data-decision-id={decision.pending.id}
      data-error={errored ? "true" : "false"}
      data-inflight={inFlightAction ?? ""}
      role="status"
      aria-live="polite"
    >
      <div className="extension-health-prompt__body">
        <strong className="extension-health-prompt__title">
          {t("extensionHealth.title", { name: primaryName })}
        </strong>
        <span className="extension-health-prompt__reason">{decision.pending.reason}</span>
        {cohortNames.length > 0 ? (
          <span className="extension-health-prompt__cohort">
            {t("extensionHealth.cohort")}: {cohortNames.join(", ")}
          </span>
        ) : null}
        {errored ? (
          <span className="extension-health-prompt__error" role="alert">
            {t("extensionHealth.error")}
          </span>
        ) : null}
        <div className="extension-health-prompt__actions">
          <button
            type="button"
            className="extension-health-prompt__action extension-health-prompt__action--danger"
            data-action="disable"
            disabled={inFlightAction !== null}
            onClick={() => submit("disable")}
          >
            {inFlightAction === "disable" ? t("extensionHealth.working") : t("extensionHealth.disable")}
          </button>
          <button
            type="button"
            className="extension-health-prompt__action"
            data-action="keep-enabled"
            disabled={inFlightAction !== null}
            onClick={() => submit("keep_enabled")}
          >
            {inFlightAction === "keep_enabled" ? t("extensionHealth.working") : t("extensionHealth.keepEnabled")}
          </button>
        </div>
      </div>
    </article>
  );
}

/** Self-contained global prompt: owns its fetch lifecycle so the only App
 * integration is rendering this once near the toast stack. Renders nothing
 * when there is no pending decision. */
export function ExtensionHealthPromptContainer() {
  const { decision, submit } = useExtensionHealthDecision();
  if (!decision) return null;
  return <ExtensionHealthPrompt decision={decision} onSubmit={submit} />;
}
