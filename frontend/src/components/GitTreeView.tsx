import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { API } from "../api";
import { buildGitGraphRows, graphLaneCount, type GitCommit, type GitGraphRow } from "../utils/gitGraph";
import Icon from "./Icon";

interface GitTreeResponse {
  is_git: boolean;
  branch?: string;
  dirty_count?: number;
  commits?: GitCommit[];
}

interface Props {
  cwd: string;
  nodeId: string;
  onClose: () => void;
}

const MAX_GRAPH_WIDTH = 116;
const GRAPH_PADDING = 12;
const ROW_HEIGHT = 64;
const NODE_Y = 23;
const LANE_COLORS = ["#8b7cf6", "#40bfa5", "#e7a84b", "#dd6f98", "#5aa6e8", "#a8bd55"];

function laneX(lane: number, laneGap: number) {
  return GRAPH_PADDING + lane * laneGap;
}

function laneColor(lane: number) {
  return LANE_COLORS[lane % LANE_COLORS.length];
}

function pathBetween(fromLane: number, fromY: number, toLane: number, toY: number, laneGap: number) {
  const fromX = laneX(fromLane, laneGap);
  const toX = laneX(toLane, laneGap);
  if (fromX === toX) return `M ${fromX} ${fromY} L ${toX} ${toY}`;
  const midpoint = fromY + (toY - fromY) / 2;
  return `M ${fromX} ${fromY} C ${fromX} ${midpoint}, ${toX} ${midpoint}, ${toX} ${toY}`;
}

function GitGraphCell({ row, width, laneGap }: { row: GitGraphRow; width: number; laneGap: number }) {
  const parentEdges = row.commit.parents.flatMap((parent) => {
    const parentLane = row.lanesAfter.indexOf(parent);
    return parentLane < 0 ? [] : [{ parent, parentLane }];
  });

  return (
    <svg
      className="git-tree-graph"
      width={width}
      height={ROW_HEIGHT}
      viewBox={`0 0 ${width} ${ROW_HEIGHT}`}
      aria-hidden="true"
    >
      {row.lanesBefore.map((hash, beforeLane) => {
        if (beforeLane === row.lane) return null;
        const afterLane = row.lanesAfter.indexOf(hash);
        if (afterLane < 0) return null;
        return (
          <path
            key={`pass-${hash}`}
            d={pathBetween(beforeLane, 0, afterLane, ROW_HEIGHT, laneGap)}
            stroke={laneColor(afterLane)}
          />
        );
      })}
      {!row.isNewTip && (
        <path
          d={pathBetween(row.lane, 0, row.lane, NODE_Y, laneGap)}
          stroke={laneColor(row.lane)}
        />
      )}
      {parentEdges.map(({ parent, parentLane }) => (
        <path
          key={`parent-${parent}`}
          d={pathBetween(row.lane, NODE_Y, parentLane, ROW_HEIGHT, laneGap)}
          stroke={laneColor(parentLane)}
        />
      ))}
      <circle
        cx={laneX(row.lane, laneGap)}
        cy={NODE_Y}
        r="5"
        fill="var(--bg-primary)"
        stroke={laneColor(row.lane)}
        strokeWidth="3"
      />
    </svg>
  );
}

function refLabel(ref: string) {
  return ref.replace(/^HEAD -> /, "").replace(/^tag: /, "");
}

export function GitTreeView({ cwd, nodeId, onClose }: Props) {
  const { t, i18n } = useTranslation();
  const [data, setData] = useState<GitTreeResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [requestVersion, setRequestVersion] = useState(0);

  const requestTree = useCallback(async (signal: AbortSignal) => {
    const params = new URLSearchParams({ cwd, node_id: nodeId, limit: "200" });
    const response = await fetch(`${API}/api/git-tree?${params}`, { signal });
    if (!response.ok) throw new Error(`git tree request failed (${response.status})`);
    return response.json() as Promise<GitTreeResponse>;
  }, [cwd, nodeId]);

  const refresh = useCallback(() => {
    setLoading(true);
    setError(false);
    setRequestVersion((version) => version + 1);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void requestTree(controller.signal)
      .then((nextData) => {
        setData(nextData);
        setError(false);
      })
      .catch((requestError: Error) => {
        if (requestError.name !== "AbortError") setError(true);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [requestTree, requestVersion]);

  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [onClose]);

  const commits = useMemo(() => data?.commits ?? [], [data?.commits]);
  const rows = useMemo(() => buildGitGraphRows(commits), [commits]);
  const laneCount = graphLaneCount(rows);
  const laneGap = laneCount <= 1
    ? 0
    : Math.min(18, (MAX_GRAPH_WIDTH - GRAPH_PADDING * 2) / (laneCount - 1));
  const graphWidth = GRAPH_PADDING * 2 + (laneCount - 1) * laneGap;
  const dateFormatter = useMemo(
    () => new Intl.DateTimeFormat(i18n.language, { dateStyle: "medium", timeStyle: "short" }),
    [i18n.language],
  );

  return (
    <section className="git-tree-view" aria-labelledby="git-tree-title" data-testid="git-tree-view">
      <header className="git-tree-header">
        <div className="git-tree-heading">
          <div className="git-tree-kicker">{data?.branch || cwd.split(/[\\/]/).pop()}</div>
          <h1 id="git-tree-title">{t("gitTree.title")}</h1>
          {data && !loading && (
            <div className="git-tree-summary">
              <span>{t("gitTree.commits", { count: commits.length })}</span>
              {(data.dirty_count ?? 0) > 0 && (
                <span className="git-tree-dirty">
                  {t("gitTree.changed", { count: data.dirty_count })}
                </span>
              )}
            </div>
          )}
        </div>
        <div className="git-tree-header-actions">
          <button
            type="button"
            className={`git-tree-icon-btn${loading ? " is-loading" : ""}`}
            onClick={refresh}
            disabled={loading}
            aria-label={t("gitTree.refresh")}
            title={t("gitTree.refresh")}
          >
            <Icon name="refresh" size={17} />
          </button>
          <button
            type="button"
            className="git-tree-icon-btn"
            onClick={onClose}
            aria-label={t("gitTree.close")}
            title={t("gitTree.close")}
          >
            <Icon name="x" size={18} />
          </button>
        </div>
      </header>

      <div className="git-tree-body" aria-live="polite">
        {loading && !data ? (
          <div className="git-tree-state git-tree-loading" role="status">
            <span className="git-tree-loading-mark" />
            {t("gitTree.loading")}
          </div>
        ) : error ? (
          <div className="git-tree-state">
            <span>{t("gitTree.loadFailed")}</span>
            <button
              type="button"
              className="git-tree-retry"
              onClick={refresh}
            >
              {t("gitTree.retry")}
            </button>
          </div>
        ) : commits.length === 0 ? (
          <div className="git-tree-state">{t("gitTree.empty")}</div>
        ) : (
          <div className={`git-tree-list${loading ? " is-refreshing" : ""}`}>
            {rows.map((row) => (
              <article className="git-tree-row" key={row.commit.hash}>
                <GitGraphCell row={row} width={graphWidth} laneGap={laneGap} />
                <div className="git-tree-commit">
                  <div className="git-tree-subject-row">
                    <h2>{row.commit.subject}</h2>
                    <code>{row.commit.hash.slice(0, 7)}</code>
                  </div>
                  {row.commit.refs.length > 0 && (
                    <div className="git-tree-refs">
                      {row.commit.refs.map((ref) => (
                        <span className={ref.startsWith("HEAD -> ") ? "is-head" : ""} key={ref}>
                          {refLabel(ref)}
                        </span>
                      ))}
                    </div>
                  )}
                  <div className="git-tree-meta">
                    <span>{row.commit.author}</span>
                    <time dateTime={row.commit.authored_at}>
                      {dateFormatter.format(new Date(row.commit.authored_at))}
                    </time>
                  </div>
                </div>
              </article>
            ))}
          </div>
        )}
      </div>
    </section>
  );
}
