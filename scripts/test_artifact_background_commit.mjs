#!/usr/bin/env node
import { spawnSync } from "node:child_process";
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import {
  cleanGeneratedArtifacts,
  commitGeneratedArtifacts,
  sourceCommitState,
} from "./artifact-background-commit.mjs";

function fail(message) {
  throw new Error(message);
}

function git(cwd, args) {
  const result = spawnSync("git", args, {
    cwd,
    encoding: "utf8",
  });
  if (result.status !== 0) {
    fail((result.stderr || result.stdout || `git ${args.join(" ")} failed`).trim());
  }
  return result.stdout.trim();
}

function write(path, contents) {
  writeFileSync(path, contents);
}

const repo = mkdtempSync(join(tmpdir(), "bc-artifact-commit-"));

try {
  const workerSource = readFileSync(new URL("./rebuild-artifacts-worker.mjs", import.meta.url), "utf8");
  const waitIndex = workerSource.indexOf("waitForSourceCommit();");
  const apkRunIndex = workerSource.indexOf('run("rebuild-android-apk.mjs")');
  if (waitIndex === -1 || apkRunIndex === -1 || waitIndex > apkRunIndex) {
    fail("expected background worker to wait for source commit before rebuilding APK");
  }

  git(repo, ["init"]);
  git(repo, ["config", "user.email", "test@example.invalid"]);
  git(repo, ["config", "user.name", "Test User"]);
  mkdirSync(join(repo, "desktop"), { recursive: true });
  mkdirSync(join(repo, "marketing", "better-agent", "downloads"), { recursive: true });
  write(join(repo, "desktop", "_version.py"), '__version__ = "0.1.1"\n');
  write(join(repo, "marketing", "better-agent", "downloads", "BetterAgent-macOS-arm64.dmg"), "dmg-v1");
  write(join(repo, "marketing", "better-agent", "downloads", "SHA256SUMS.txt"), "sha-v1  BetterAgent-macOS-arm64.dmg\n");
  write(join(repo, "marketing", "better-agent", "index.html"), '<a href="./downloads/BetterAgent-macOS-arm64.dmg?v=0.1.1">Download</a>');
  write(join(repo, "marketing", "better-agent", "styles.css"), "body { color: black; }\n");
  write(join(repo, "unrelated.txt"), "base");
  git(repo, ["add", "."]);
  git(repo, ["commit", "-m", "initial"]);
  const baseHead = git(repo, ["rev-parse", "HEAD"]);
  if (sourceCommitState(repo, baseHead) !== "pending") {
    fail("expected source commit state to be pending at base HEAD");
  }

  write(join(repo, "desktop", "_version.py"), '__version__ = "0.1.2"\n');
  write(join(repo, "marketing", "better-agent", "downloads", "BetterAgent-macOS-arm64.dmg"), "dmg-v2");
  write(join(repo, "marketing", "better-agent", "downloads", "SHA256SUMS.txt"), "sha-v2  BetterAgent-macOS-arm64.dmg\n");
  write(join(repo, "marketing", "better-agent", "index.html"), '<a href="./downloads/BetterAgent-macOS-arm64.dmg?v=0.1.2">Download</a>');
  write(join(repo, "marketing", "better-agent", "styles.css"), "body { color: blue; }\n");
  write(join(repo, "unrelated.txt"), "second staged unrelated");
  git(repo, ["add", "unrelated.txt"]);
  git(repo, ["commit", "-m", "source"]);
  if (sourceCommitState(repo, baseHead) !== "ready") {
    fail("expected exactly one source commit to be ready");
  }
  write(join(repo, "unrelated.txt"), "staged unrelated");
  git(repo, ["add", "unrelated.txt"]);

  const result = commitGeneratedArtifacts({ cwd: repo });
  if (!result.committed) fail("expected generated artifacts to be committed");

  const committedFiles = git(repo, ["diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"])
    .split("\n")
    .filter(Boolean)
    .sort();
  const expectedFiles = [
    "desktop/_version.py",
    "marketing/better-agent/downloads/BetterAgent-macOS-arm64.dmg",
    "marketing/better-agent/downloads/SHA256SUMS.txt",
    "marketing/better-agent/index.html",
    "marketing/better-agent/styles.css",
  ];
  if (JSON.stringify(committedFiles) !== JSON.stringify(expectedFiles)) {
    fail(`expected only generated artifacts in commit, got ${committedFiles.join(", ")}`);
  }

  const status = git(repo, ["status", "--porcelain"]);
  if (!status.includes("M  unrelated.txt")) {
    fail(`expected unrelated staged change to remain staged, got ${status}`);
  }

  const unchanged = commitGeneratedArtifacts({ cwd: repo });
  if (unchanged.committed) fail("expected no commit when generated artifacts are unchanged");

  write(join(repo, "marketing", "better-agent", "downloads", "SHA256SUMS.txt"), "sha-v3  BetterAgent-macOS-arm64.dmg\n");
  const onePath = commitGeneratedArtifacts({ cwd: repo });
  if (!onePath.committed) fail("expected changed artifact to be committed");
  const onePathFiles = git(repo, ["diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"])
    .split("\n")
    .filter(Boolean);
  if (JSON.stringify(onePathFiles) !== JSON.stringify(["marketing/better-agent/downloads/SHA256SUMS.txt"])) {
    fail(`expected only changed artifact path in commit, got ${onePathFiles.join(", ")}`);
  }

  write(join(repo, "desktop", "_version.py"), '__version__ = "manual-user-edit"\n');
  const cleanPaths = cleanGeneratedArtifacts(repo);
  if (cleanPaths.includes("desktop/_version.py")) {
    fail("expected pre-existing dirty desktop version to be excluded from auto-commit paths");
  }

  git(repo, ["commit", "-m", "second source"]);
  if (sourceCommitState(repo, baseHead) !== "advanced_too_far") {
    fail("expected source commit state to reject more than one commit ahead");
  }
} finally {
  rmSync(repo, { recursive: true, force: true });
}
