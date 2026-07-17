#!/usr/bin/env node
import { execFileSync, spawn, spawnSync } from "node:child_process";
import { mkdirSync, openSync, closeSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { isThrottled, recordRun } from "./artifact-throttle.mjs";
import { androidRebuildDecision } from "./android-rebuild-policy.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, "..");
const LOG_DIR = execFileSync("git", ["rev-parse", "--git-path", "bc-artifacts"], {
  cwd: ROOT,
  encoding: "utf8",
}).trim();

function currentHead() {
  const result = spawnSync("git", ["rev-parse", "HEAD"], {
    cwd: ROOT,
    encoding: "utf8",
  });
  return result.status === 0 ? result.stdout.trim() : "";
}

function stagedFiles() {
  const child = spawn("git", ["diff", "--cached", "--name-only", "--diff-filter=ACMR"], {
    cwd: ROOT,
    stdio: ["ignore", "pipe", "inherit"],
  });
  return new Promise((resolve, reject) => {
    let out = "";
    child.stdout.on("data", (chunk) => { out += chunk; });
    child.on("error", reject);
    child.on("close", (code) => {
      if (code !== 0) {
        reject(new Error(`git diff --cached failed with ${code}`));
        return;
      }
      resolve(out.trim() ? out.trim().split("\n") : []);
    });
  });
}

const files = await stagedFiles();
if (files.length === 0) {
  console.log("[artifacts] no staged files — skipping background artifact rebuild.");
  process.exit(0);
}

const androidDecision = androidRebuildDecision(files);

// Throttle: at most one rebuild scheduled per window. BA_FORCE_ARTIFACT_REBUILD=1 bypasses.
const stampPath = join(LOG_DIR, "last-scheduled");
const now = Date.now();
if (
  process.env.BA_FORCE_ARTIFACT_REBUILD !== "1"
  && !androidDecision.rebuild
  && isThrottled(stampPath, now)
) {
  console.log("[artifacts] throttled — a rebuild was scheduled within the last 10 min; skipping.");
  process.exit(0);
}
recordRun(stampPath, now);

mkdirSync(LOG_DIR, { recursive: true });
const stamp = new Date().toISOString().replace(/[:.]/g, "-");
const logPath = join(LOG_DIR, `${stamp}.log`);
const fd = openSync(logPath, "a");

const child = spawn(
  process.execPath,
  [join(HERE, "rebuild-artifacts-worker.mjs")],
  {
    cwd: ROOT,
    detached: true,
    stdio: ["ignore", fd, fd],
    env: {
      ...process.env,
      BA_ARTIFACT_BASE_HEAD: currentHead(),
      BA_ARTIFACT_BACKGROUND: "1",
      BA_STAGED_FILES_JSON: JSON.stringify(files),
    },
  },
);
child.unref();
closeSync(fd);

const reason = androidDecision.rebuild
  ? `Android inputs changed: ${androidDecision.relevantPaths.join(", ")}`
  : "non-Android artifact check";
console.log(`[artifacts] rebuild scheduled in background (${reason}); log: ${logPath}`);
