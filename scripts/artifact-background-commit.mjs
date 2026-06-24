import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, "..");

export const GENERATED_ARTIFACT_PATHS = [
  "desktop/_version.py",
  "marketing/better-agent/downloads/BetterAgent-macOS-arm64.dmg",
  "marketing/better-agent/downloads/SHA256SUMS.txt",
  "marketing/better-agent/index.html",
];

function git(cwd, args, options = {}) {
  return spawnSync("git", args, {
    cwd,
    encoding: "utf8",
    ...options,
  });
}

function gitWithIndex(cwd, indexFile, args) {
  return git(cwd, args, {
    env: {
      ...process.env,
      BA_SKIP_ARTIFACT_BACKGROUND: "1",
      GIT_INDEX_FILE: indexFile,
    },
  });
}

export function cleanGeneratedArtifacts(cwd = ROOT, paths = GENERATED_ARTIFACT_PATHS) {
  return paths.filter((path) => {
    const result = git(cwd, ["status", "--porcelain", "--", path]);
    if (result.status !== 0) {
      throw new Error((result.stderr || result.stdout || `git status failed for ${path}`).trim());
    }
    return !result.stdout.trim();
  });
}

export function changedGeneratedArtifacts(cwd = ROOT, paths = GENERATED_ARTIFACT_PATHS) {
  if (paths.length === 0) return [];
  const result = git(cwd, ["status", "--porcelain", "--", ...paths]);
  if (result.status !== 0) {
    throw new Error((result.stderr || result.stdout || "git status failed").trim());
  }
  const changed = new Set(
    result.stdout
      .split("\n")
      .filter((line) => line.trim())
      .map((line) => line.slice(3))
      .filter(Boolean),
  );
  return paths.filter((path) => changed.has(path));
}

export function commitGeneratedArtifacts({
  cwd = ROOT,
  paths = GENERATED_ARTIFACT_PATHS,
  message = "Rebuild generated artifacts",
  log = () => {},
} = {}) {
  const changedPaths = changedGeneratedArtifacts(cwd, paths);
  if (changedPaths.length === 0) {
    log("no generated artifact changes to commit.");
    return { committed: false, paths: changedPaths };
  }

  const tmp = mkdtempSync(join(tmpdir(), "bc-artifact-index-"));
  const indexFile = join(tmp, "index");
  try {
    let result = gitWithIndex(cwd, indexFile, ["read-tree", "HEAD"]);
    if (result.status !== 0) {
      throw new Error((result.stderr || result.stdout || "git read-tree failed").trim());
    }
    result = gitWithIndex(cwd, indexFile, ["add", "--", ...changedPaths]);
    if (result.status !== 0) {
      throw new Error((result.stderr || result.stdout || "git artifact add failed").trim());
    }
    result = gitWithIndex(cwd, indexFile, ["commit", "--no-verify", "-m", message]);
    if (result.status !== 0) {
      throw new Error((result.stderr || result.stdout || "git artifact commit failed").trim());
    }
    result = git(cwd, ["reset", "-q", "HEAD", "--", ...changedPaths]);
    if (result.status !== 0) {
      throw new Error((result.stderr || result.stdout || "git artifact index refresh failed").trim());
    }
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
  log(`committed generated artifacts: ${changedPaths.join(", ")}`);
  return { committed: true, paths: changedPaths };
}

export function sourceCommitState(cwd = ROOT, baseHead = "") {
  if (!baseHead) return "unknown";
  const ancestry = git(cwd, ["merge-base", "--is-ancestor", baseHead, "HEAD"]);
  if (ancestry.status !== 0) return "not_descended";
  const count = git(cwd, ["rev-list", "--count", `${baseHead}..HEAD`]);
  if (count.status !== 0) {
    throw new Error((count.stderr || count.stdout || "git rev-list failed").trim());
  }
  const commitsAhead = Number(count.stdout.trim());
  if (commitsAhead === 0) return "pending";
  if (commitsAhead === 1) return "ready";
  return "advanced_too_far";
}
