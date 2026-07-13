/** Worktree-aware project membership for the frontend.

The backend groups every git worktree of one repo into a single project
and matches a session to a project by git COMMON DIR. The frontend cannot
run git, so it mirrors that matching with a longest-worktree-root-prefix
attribution: a session belongs to the project whose worktree root is the
longest prefix of the session's cwd. This correctly keeps a NESTED repo
(a separate repo checked out inside a worktree) attributed to its own
project, because the nested repo's root is longer than the parent's.

`setGroupedProjects` is called by App whenever `/api/projects` returns a
fresh list; the helpers below read the resulting index. All scattered
frontend mirrors of project membership (uiSelection routing, the session
registry badge, the optimistic local-insert check) resolve through here
so they stay in parity with the backend's common-dir matching.
*/

import type { Project } from "../types";

/** Current grouped project list, indexed for longest-prefix lookup. */
interface ProjectIndex {
  /** All worktree roots across all projects, sorted longest-first so the
   * first prefix match is the most specific (handles nested repos). */
  roots: { root: string; projectPath: string; nodeId: string }[];
  /** (path, nodeId) -> Project, for exact-path and metadata lookups. */
  byPath: Map<string, Project>;
}

let _index: ProjectIndex = { roots: [], byPath: new Map() };
/** cwd -> project cache, keyed by `${nodeId}::${cwd}`. Cleared whenever the
 * project index changes. The session registry's per-delta recompute calls
 * projectForCwd for every session; this keeps it O(1) after warmup instead
 * of O(roots) per session. */
let _cwdCache: Map<string, Project | undefined> = new Map();

const PRIMARY = "primary";

function nodeKey(path: string, nodeId: string): string {
  return `${nodeId || PRIMARY}::${path}`;
}

function projectRoots(project: Project): string[] {
  const roots: string[] = [];
  const seen = new Set<string>();
  for (const w of project.worktrees || []) {
    if (w.path && !seen.has(w.path)) {
      seen.add(w.path);
      roots.push(w.path);
    }
  }
  if (project.path && !seen.has(project.path)) {
    roots.push(project.path);
  }
  return roots;
}

export function setGroupedProjects(projects: Project[]): void {
  const byPath = new Map<string, Project>();
  const roots: { root: string; projectPath: string; nodeId: string }[] = [];
  for (const p of projects) {
    const nodeId = p.node_id || PRIMARY;
    byPath.set(nodeKey(p.path, nodeId), p);
    for (const root of projectRoots(p)) {
      roots.push({ root, projectPath: p.path, nodeId });
    }
  }
  // Longest root first so the first prefix hit is the most specific.
  roots.sort((a, b) => b.root.length - a.root.length);
  _index = { roots, byPath };
  _cwdCache = new Map();
}

/** The project a session cwd belongs to, by longest worktree-root prefix
 * (then exact path). Returns undefined when the cwd matches no project. */
export function projectForCwd(
  cwd: string | undefined,
  nodeId: string | undefined,
): Project | undefined {
  if (!cwd) return undefined;
  const node = nodeId || PRIMARY;
  const ck = `${node}::${cwd}`;
  if (_cwdCache.has(ck)) return _cwdCache.get(ck);
  let result: Project | undefined;
  for (const { root, projectPath, nodeId: rootNode } of _index.roots) {
    if (rootNode !== node) continue;
    if (cwd === root || cwd.startsWith(root + "/")) {
      result = _index.byPath.get(nodeKey(projectPath, node));
      break;
    }
  }
  if (!result) {
    // Legacy / non-git fallback: exact path registration.
    result = _index.byPath.get(nodeKey(cwd, node));
  }
  _cwdCache.set(ck, result);
  return result;
}

/** Does a session with this cwd belong to the project at `(path, nodeId)`?
 * Mirrors backend session_matches_project for the routing/insert paths.
 *
 * When the grouped-project index is populated, attribution is by longest
 * worktree-root prefix — so a session in any worktree/subdir of the repo
 * matches, while a NESTED repo's session does NOT (it owns a longer root).
 * When the index is empty (no `/api/projects` load yet, or a unit test)
 * this falls back to an exact cwd===path match, preserving the
 * pre-worktree behavior for the pure routing helpers. */
export function belongsToProjectPath(
  cwd: string | undefined,
  path: string,
  nodeId: string | undefined,
): boolean {
  if (!cwd) return false;
  const node = nodeId || PRIMARY;
  if (_index.roots.length) {
    const owner = projectForCwd(cwd, node);
    return !!owner && owner.path === path && (owner.node_id || PRIMARY) === node;
  }
  return cwd === path;
}

export function worktreeRootsFor(path: string, nodeId: string | undefined): string[] {
  const proj = _index.byPath.get(nodeKey(path, nodeId || PRIMARY));
  return proj ? projectRoots(proj) : [];
}
