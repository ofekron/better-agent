import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

export interface RealGitRepo {
  /** Absolute path to the repo as created on disk. The backend's
   * project_store normalizes/resolves paths (symlinks, trailing slashes) on
   * `POST /api/projects`, so callers should match UI state against the
   * `path` field of that response, not this raw value, when they may differ
   * (e.g. macOS's `/tmp` -> `/private/tmp` symlink). */
  dir: string;
  /** Short (7-char) hash of the initial commit, as `git` itself reports it. */
  initialCommitShortHash: string;
  cleanup(): void;
}

function git(dir: string, args: string[]): string {
  return execFileSync("git", args, { cwd: dir, encoding: "utf-8" }).trim();
}

/**
 * Creates a REAL git repository on disk (real `git` subprocess, no mocking)
 * with one commit on a deterministic `main` branch, so full-stack git tests
 * can assert on real `git`-reported state (branch name, commit hash/subject,
 * dirty file count) surfaced through the real backend's git RPC handlers.
 */
export function createRealGitRepo(commitSubject = "initial commit"): RealGitRepo {
  const dir = mkdtempSync(path.join(tmpdir(), "ba-fullstack-git-"));
  git(dir, ["init", "-b", "main"]);
  git(dir, ["config", "user.email", "fullstack-test@example.com"]);
  git(dir, ["config", "user.name", "Full Stack Test"]);
  git(dir, ["config", "commit.gpgsign", "false"]);
  writeFileSync(path.join(dir, "README.md"), "# fullstack git test repo\n");
  git(dir, ["add", "README.md"]);
  git(dir, ["commit", "-m", commitSubject]);
  const initialCommitShortHash = git(dir, ["rev-parse", "--short=7", "HEAD"]);

  return {
    dir,
    initialCommitShortHash,
    cleanup(): void {
      rmSync(dir, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
    },
  };
}

/** Writes a real untracked file into the repo so `git status` reports it —
 * a real dirty-working-tree fixture, not a simulated one. */
export function addUntrackedFile(repo: RealGitRepo, name: string, content: string): void {
  writeFileSync(path.join(repo.dir, name), content);
}

export interface PlainDir {
  dir: string;
  cleanup(): void;
}

/**
 * Creates a REAL plain directory on disk with a real file in it, but
 * deliberately WITHOUT `git init` — a fixture for exercising the
 * non-git-repo path of the git-status/git-tree RPCs (`is_git: false`),
 * as distinct from a real git repo.
 */
export function createPlainDir(): PlainDir {
  const dir = mkdtempSync(path.join(tmpdir(), "ba-fullstack-nongit-"));
  writeFileSync(path.join(dir, "readme.txt"), "not a git repo\n");

  return {
    dir,
    cleanup(): void {
      rmSync(dir, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
    },
  };
}
