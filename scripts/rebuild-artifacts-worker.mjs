#!/usr/bin/env node
import { spawnSync } from "node:child_process";
import { appendFileSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import {
  cleanGeneratedArtifacts,
  commitGeneratedArtifacts,
  sourceCommitState,
} from "./artifact-background-commit.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, "..");

function log(message) {
  appendFileSync(1, `[artifacts] ${message}\n`);
}

function run(script) {
  const started = new Date().toISOString();
  log(`${script} started at ${started}`);
  const result = spawnSync(process.execPath, [join(HERE, script)], {
    cwd: ROOT,
    env: process.env,
    stdio: "inherit",
  });
  const ended = new Date().toISOString();
  if (result.status !== 0) {
    log(`${script} failed at ${ended} with status ${result.status ?? "signal " + result.signal}`);
    process.exit(result.status ?? 1);
  }
  log(`${script} finished at ${ended}`);
}

function sleep(ms) {
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
}

function waitForSourceCommit() {
  const baseHead = process.env.BA_ARTIFACT_BASE_HEAD || "";
  if (!baseHead) return;
  for (let attempt = 0; attempt < 120; attempt += 1) {
    const state = sourceCommitState(ROOT, baseHead);
    if (state === "ready") {
      return;
    }
    if (state === "not_descended") {
      log("HEAD is not descended from the commit that scheduled artifact rebuild; leaving artifacts dirty.");
      process.exit(1);
    }
    if (state === "advanced_too_far") {
      log("multiple commits landed before artifact rebuild finished; leaving artifacts dirty.");
      process.exit(1);
    }
    sleep(1000);
  }
  log("source commit did not complete before artifact commit timeout; leaving artifacts dirty.");
  process.exit(1);
}

if (process.env.BA_ARTIFACT_BACKGROUND === "1") {
  waitForSourceCommit();
}

const artifactPathsCleanBeforeRebuild = cleanGeneratedArtifacts(ROOT);
run("rebuild-android-apk.mjs");
run("rebuild-desktop-artifacts.mjs");
run("sync-marketing-desktop-download.mjs");

if (process.env.BA_ARTIFACT_BACKGROUND === "1") {
  commitGeneratedArtifacts({
    cwd: ROOT,
    paths: artifactPathsCleanBeforeRebuild,
    log,
  });
}
