import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { A2AAgent } from "../types";
import { createA2AAgent, deleteA2AAgent, delegateToA2AAgent, probeA2AAgent } from "../api";
import { useA2AAgents } from "../hooks/useA2AAgents";

// A2A (Agent2Agent) remote-agent registry: add/remove/probe outbound-only
// remote agents, redacted-secret display, and a delegate action that
// projects a real worker panel on a chosen session via
// `POST /api/a2a/agents/{id}/delegate`. Backend-owned state via
// useA2AAgents (REST snapshot + a2a_registry_changed WS frames).

type RowPending = "probe" | "remove" | "delegate" | null;

export function A2AAgentsSettings() {
  const { t } = useTranslation();
  const { agents, loading, error: loadError, refresh } = useA2AAgents();

  const [showAdd, setShowAdd] = useState(false);
  const [name, setName] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [authHeaderName, setAuthHeaderName] = useState("");
  const [authSecret, setAuthSecret] = useState("");
  const [adding, setAdding] = useState(false);
  const [addError, setAddError] = useState("");

  const [rowPending, setRowPending] = useState<Record<string, RowPending>>({});
  const [rowError, setRowError] = useState<Record<string, string>>({});
  const [delegateOpenId, setDelegateOpenId] = useState<string | null>(null);
  const [delegateSessionId, setDelegateSessionId] = useState("");
  const [delegateInstructions, setDelegateInstructions] = useState("");
  const [delegateResult, setDelegateResult] = useState<Record<string, string>>({});

  const setPending = (id: string, action: RowPending) =>
    setRowPending((prev) => ({ ...prev, [id]: action }));
  const setError = (id: string, message: string) =>
    setRowError((prev) => ({ ...prev, [id]: message }));

  const submitAdd = async () => {
    setAdding(true);
    setAddError("");
    try {
      await createA2AAgent({
        name: name.trim(),
        base_url: baseUrl.trim(),
        auth_header_name: authHeaderName.trim(),
        auth_secret: authSecret,
      });
      setName("");
      setBaseUrl("");
      setAuthHeaderName("");
      setAuthSecret("");
      setShowAdd(false);
      refresh();
    } catch (e) {
      setAddError(e instanceof Error ? e.message : String(e));
    } finally {
      setAdding(false);
    }
  };

  const probe = async (agent: A2AAgent) => {
    setPending(agent.id, "probe");
    setError(agent.id, "");
    try {
      await probeA2AAgent(agent.id);
      refresh();
    } catch (e) {
      setError(agent.id, e instanceof Error ? e.message : String(e));
    } finally {
      setPending(agent.id, null);
    }
  };

  const remove = async (agent: A2AAgent) => {
    if (!confirm(t("a2aAgents.removeConfirm", { name: agent.name }))) return;
    setPending(agent.id, "remove");
    setError(agent.id, "");
    try {
      await deleteA2AAgent(agent.id);
      refresh();
    } catch (e) {
      setError(agent.id, e instanceof Error ? e.message : String(e));
      setPending(agent.id, null);
    }
  };

  const submitDelegate = async (agent: A2AAgent) => {
    setPending(agent.id, "delegate");
    setError(agent.id, "");
    try {
      const result = await delegateToA2AAgent(agent.id, {
        app_session_id: delegateSessionId.trim(),
        instructions: delegateInstructions.trim(),
      });
      setDelegateResult((prev) => ({ ...prev, [agent.id]: result.delegation_id }));
      setDelegateOpenId(null);
      setDelegateSessionId("");
      setDelegateInstructions("");
    } catch (e) {
      setError(agent.id, e instanceof Error ? e.message : String(e));
    } finally {
      setPending(agent.id, null);
    }
  };

  if (loading && agents.length === 0) {
    return <div className="a2a-agents-settings">{t("common.loading", "Loading…")}</div>;
  }

  return (
    <div className="a2a-agents-settings">
      <p className="settings-hint">{t("a2aAgents.description")}</p>
      {loadError && (
        <div className="harness-settings-editor-error" role="alert">{loadError}</div>
      )}

      <div className="a2a-agents-list">
        {agents.length === 0 && !showAdd && (
          <p className="settings-hint">{t("a2aAgents.empty")}</p>
        )}
        {agents.map((agent) => {
          const pending = rowPending[agent.id] ?? null;
          const busy = pending !== null;
          return (
            <div
              key={agent.id}
              className={`a2a-agent-row${busy ? " is-disabled" : ""}`}
              data-testid={`a2a-agent-row-${agent.id}`}
            >
              <div className="a2a-agent-row-main">
                <div className="a2a-agent-row-name">{agent.name}</div>
                <div className="a2a-agent-row-url">{agent.base_url}</div>
                <div className="a2a-agent-row-meta">
                  {agent.has_auth_secret && (
                    <span className="a2a-agent-secret-badge">
                      {t("a2aAgents.authConfigured", { header: agent.auth_header_name })}
                    </span>
                  )}
                  {agent.last_probe_ok === true && (
                    <span className="a2a-agent-status-ok">{t("a2aAgents.reachable")}</span>
                  )}
                  {agent.last_probe_ok === false && (
                    <span className="a2a-agent-status-fail" title={agent.last_probe_error ?? ""}>
                      {t("a2aAgents.unreachable")}
                    </span>
                  )}
                  {agent.last_probe_ok === null && (
                    <span className="a2a-agent-status-unknown">{t("a2aAgents.notProbed")}</span>
                  )}
                </div>
                {rowError[agent.id] && (
                  <div className="harness-settings-editor-error" role="alert">
                    {rowError[agent.id]}
                  </div>
                )}
                {delegateResult[agent.id] && (
                  <div className="a2a-agent-delegate-result">
                    {t("a2aAgents.delegateStarted", { id: delegateResult[agent.id] })}
                  </div>
                )}
              </div>
              <div className="a2a-agent-row-actions">
                <button
                  type="button"
                  className="btn-secondary"
                  disabled={busy}
                  data-testid={`a2a-agent-probe-${agent.id}`}
                  onClick={() => void probe(agent)}
                >
                  {pending === "probe" && <span className="retrying-spinner" aria-hidden="true" />}
                  {pending === "probe" ? t("a2aAgents.probing") : t("a2aAgents.probe")}
                </button>
                <button
                  type="button"
                  className="btn-secondary"
                  disabled={busy}
                  data-testid={`a2a-agent-delegate-toggle-${agent.id}`}
                  onClick={() => setDelegateOpenId(delegateOpenId === agent.id ? null : agent.id)}
                >
                  {t("a2aAgents.delegate")}
                </button>
                <button
                  type="button"
                  className="btn-danger"
                  disabled={busy}
                  data-testid={`a2a-agent-remove-${agent.id}`}
                  onClick={() => void remove(agent)}
                >
                  {pending === "remove" ? t("a2aAgents.removing") : t("a2aAgents.remove")}
                </button>
              </div>
              {delegateOpenId === agent.id && (
                <div className="a2a-agent-delegate-form">
                  <div className="setup-field">
                    <label>{t("a2aAgents.delegateSessionLabel")}</label>
                    <input
                      type="text"
                      value={delegateSessionId}
                      onChange={(e) => setDelegateSessionId(e.target.value)}
                      placeholder={t("a2aAgents.delegateSessionPlaceholder")}
                      spellCheck={false}
                    />
                  </div>
                  <div className="setup-field">
                    <label>{t("a2aAgents.delegateInstructionsLabel")}</label>
                    <textarea
                      value={delegateInstructions}
                      onChange={(e) => setDelegateInstructions(e.target.value)}
                      rows={3}
                    />
                  </div>
                  <div className="provider-edit-actions">
                    <button
                      type="button"
                      className="setup-save-btn"
                      disabled={busy || !delegateSessionId.trim() || !delegateInstructions.trim()}
                      onClick={() => void submitDelegate(agent)}
                    >
                      {pending === "delegate" && <span className="retrying-spinner" aria-hidden="true" />}
                      {pending === "delegate" ? t("a2aAgents.delegating") : t("a2aAgents.delegateSubmit")}
                    </button>
                    <button
                      type="button"
                      className="btn-secondary"
                      disabled={busy}
                      onClick={() => setDelegateOpenId(null)}
                    >
                      {t("a2aAgents.cancel")}
                    </button>
                  </div>
                </div>
              )}
            </div>
          );
        })}
      </div>

      <div className="setup-divider" />

      {!showAdd && (
        <button
          type="button"
          className="btn-secondary"
          data-testid="a2a-agent-add-toggle"
          onClick={() => setShowAdd(true)}
        >
          {t("a2aAgents.addCta")}
        </button>
      )}
      {showAdd && (
        <div className="a2a-agent-add-form">
          {addError && (
            <div className="harness-settings-editor-error" role="alert">{addError}</div>
          )}
          <div className="setup-field">
            <label>{t("a2aAgents.nameLabel")}</label>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              data-testid="a2a-agent-name-input"
              spellCheck={false}
            />
          </div>
          <div className="setup-field">
            <label>{t("a2aAgents.baseUrlLabel")}</label>
            <input
              type="text"
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              data-testid="a2a-agent-url-input"
              placeholder="https://agent.example.com"
              spellCheck={false}
            />
          </div>
          <div className="setup-field">
            <label>{t("a2aAgents.authHeaderLabel")}</label>
            <input
              type="text"
              value={authHeaderName}
              onChange={(e) => setAuthHeaderName(e.target.value)}
              placeholder="Authorization"
              spellCheck={false}
            />
          </div>
          <div className="setup-field">
            <label>{t("a2aAgents.authSecretLabel")}</label>
            <input
              type="password"
              value={authSecret}
              onChange={(e) => setAuthSecret(e.target.value)}
              autoComplete="off"
              data-testid="a2a-agent-secret-input"
            />
          </div>
          <div className="provider-edit-actions">
            <button
              type="button"
              className="setup-save-btn"
              disabled={adding || !name.trim() || !baseUrl.trim()}
              data-testid="a2a-agent-add-submit"
              onClick={() => void submitAdd()}
            >
              {adding && <span className="retrying-spinner" aria-hidden="true" />}
              {adding ? t("a2aAgents.adding") : t("a2aAgents.addSubmit")}
            </button>
            <button
              type="button"
              className="btn-secondary"
              disabled={adding}
              onClick={() => setShowAdd(false)}
            >
              {t("a2aAgents.cancel")}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
