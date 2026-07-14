import { useState, useEffect, useCallback, useRef } from "react";
import { API } from "../api";
import { runThreeStateSync } from "../progress/store";

interface GitStatus {
  is_git: boolean;
  branch?: string;
  modified?: string[];
  added?: string[];
  deleted?: string[];
  untracked?: string[];
}

interface Props {
  cwd: string;
  nodeId: string;
}

interface GitCommitResult {
  ok: boolean;
  action: "commit" | "commit-push";
  message: string;
  output?: string;
  error?: string;
  committed?: boolean;
}

export function ProjectGitStatus({ cwd, nodeId }: Props) {
  const [status, setStatus] = useState<GitStatus | null>(null);
  const [committing, setCommitting] = useState(false);
  const [pushing, setPushing] = useState(false);
  const [lastResult, setLastResult] = useState<GitCommitResult | null>(null);
  const [showResultModal, setShowResultModal] = useState(false);
  const [showCommitInput, setShowCommitInput] = useState(false);
  const [message, setMessage] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  const fetchStatus = useCallback(async () => {
    try {
      const params = new URLSearchParams({ cwd, node_id: nodeId });
      const res = await fetch(`${API}/api/git-status?${params}`);
      if (res.ok) {
        const data = await res.json();
        setStatus(data);
      }
    } catch {
      // ignore fetch errors
    }
  }, [cwd, nodeId]);

  useEffect(() => {
    fetchStatus();
    const id = setInterval(fetchStatus, 15_000);
    return () => clearInterval(id);
  }, [fetchStatus]);

  useEffect(() => {
    if (showCommitInput) inputRef.current?.focus();
  }, [showCommitInput]);

  if (!status || !status.is_git) return null;

  const dirty = [
    ...(status.modified || []),
    ...(status.added || []),
    ...(status.deleted || []),
    ...(status.untracked || []),
  ];
  const dirtyCount = dirty.length;
  const isClean = dirtyCount === 0;

  async function doCommit(andPush: boolean) {
    const msg = message.trim() || `chore: ${dirtyCount} file${dirtyCount !== 1 ? "s" : ""} changed`;
    if (andPush) setPushing(true);
    else setCommitting(true);
    try {
      const endpoint = andPush ? "/api/git-commit-and-push" : "/api/git-commit";
      const { result: data } = await runThreeStateSync({
        operationId: `project:git:${andPush ? "commit-push" : "commit"}:${nodeId}:${cwd}`,
        action: andPush ? "Commit & Push" : "Commit",
        info: cwd,
        reconcile: fetchStatus,
        mutate: async () => {
          const res = await fetch(`${API}${endpoint}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ cwd, node_id: nodeId, message: msg }),
          });
          const payload = await res.json() as GitCommitResult;
          if (!res.ok || !payload.ok) throw new Error(payload.error || `HTTP ${res.status}`);
          return payload;
        },
      });
      setLastResult({
        ok: Boolean(data.ok),
        action: andPush ? "commit-push" : "commit",
        message: msg,
        output: typeof data.output === "string" ? data.output : undefined,
        error: typeof data.error === "string" ? data.error : undefined,
        committed: Boolean(data.committed),
      });
      if (data.ok) {
        setMessage("");
        setShowCommitInput(false);
        fetchStatus();
      }
    } catch (error) {
      setLastResult({
        ok: false,
        action: andPush ? "commit-push" : "commit",
        message: msg,
        error: error instanceof Error ? error.message : "Network error",
      });
    } finally {
      setCommitting(false);
      setPushing(false);
    }
  }

  return (
    <div className="project-git-status">
      <div className="project-git-info">
        <span className="project-git-branch" title={status.branch}>
          <span className="git-branch-icon">⎇</span> {status.branch}
        </span>
        <span className={`project-git-dirty${isClean ? " clean" : ""}`}>
          {isClean ? "clean" : `${dirtyCount} changed`}
        </span>
      </div>
      {lastResult && (
        <button
          type="button"
          className={`project-git-result-btn${lastResult.ok ? " ok" : " error"}`}
          onClick={() => setShowResultModal(true)}
          title="Show last git result"
        >
          {lastResult.ok ? "Result" : "Failed"}
        </button>
      )}
      {!isClean && (
        <div className="project-git-actions">
          {showCommitInput ? (
            <div className="project-git-commit-row">
              <input
                ref={inputRef}
                className="project-git-commit-input"
                type="text"
                placeholder="Commit message…"
                value={message}
                onChange={(e) => setMessage(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !committing && !pushing) doCommit(false);
                  if (e.key === "Escape") setShowCommitInput(false);
                }}
                disabled={committing || pushing}
              />
              <button
                className="project-git-btn"
                onClick={() => doCommit(false)}
                disabled={committing || pushing}
                title="Commit"
              >
                {committing ? "…" : "✓"}
              </button>
              <button
                className="project-git-btn push"
                onClick={() => doCommit(true)}
                disabled={committing || pushing}
                title="Commit & Push"
              >
                {pushing ? "…" : "↑"}
              </button>
            </div>
          ) : (
            <button
              className="project-git-btn-open"
              onClick={() => setShowCommitInput(true)}
            >
              Commit…
            </button>
          )}
        </div>
      )}
      {showResultModal && lastResult && (
        <div className="modal-overlay" onClick={() => setShowResultModal(false)}>
          <div
            className="modal-content project-git-result-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="project-git-result-title"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="modal-header">
              <h2 id="project-git-result-title">Last Git Result</h2>
              <button
                type="button"
                className="modal-close"
                onClick={() => setShowResultModal(false)}
                aria-label="Close"
              >
                ×
              </button>
            </div>
            <div className="modal-body">
              <div className={`project-git-result-summary${lastResult.ok ? " ok" : " error"}`}>
                {lastResult.ok ? "Success" : lastResult.committed ? "Commit succeeded, push failed" : "Failed"}
              </div>
              <dl className="project-git-result-meta">
                <div>
                  <dt>Action</dt>
                  <dd>{lastResult.action === "commit-push" ? "Commit & Push" : "Commit"}</dd>
                </div>
                <div>
                  <dt>Message</dt>
                  <dd>{lastResult.message}</dd>
                </div>
              </dl>
              <pre className="project-git-result-log">
                {lastResult.output || lastResult.error || "No output."}
              </pre>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
