#!/usr/bin/env node
// Atomic frontend build. `npm run build` compiles into a temp dir, swaps it
// into dist/ with rename, then union-merges the previous build's
// content-hashed assets/* into the new dist. Content-hashed filenames are
// immutable, so keeping recent ones lets long-lived tabs still resolve their
// lazy chunks after a rebuild instead of hitting a missing chunk and
// force-reloading the app (frontend/src/lib/lazyWithRetry.ts).
//
// CLI:
//   node scripts/build-atomic.mjs                       # full atomic build
//   node scripts/build-atomic.mjs --merge-assets A B    # merge A/assets → B/assets (for tests)
import { execFileSync } from "node:child_process";
import {
  copyFileSync,
  existsSync,
  lstatSync,
  mkdirSync,
  readdirSync,
  renameSync,
  rmSync,
  statSync,
  utimesSync,
  writeFileSync,
} from "node:fs";
import { basename, dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const frontendDir = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const MERGE_MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000;

export function mergePreviousAssets(previousDist, newDist, now = Date.now()) {
  const prevAssets = join(previousDist, "assets");
  const newAssets = join(newDist, "assets");
  if (!existsSync(prevAssets)) return [];
  mkdirSync(newAssets, { recursive: true });
  const merged = [];
  for (const name of readdirSync(prevAssets)) {
    const src = join(prevAssets, name);
    const dst = join(newAssets, name);
    if (existsSync(dst)) continue;
    const st = lstatSync(src);
    if (!st.isFile()) continue;
    if (now - st.mtimeMs > MERGE_MAX_AGE_MS) continue;
    copyFileSync(src, dst);
    utimesSync(dst, st.atime, st.mtime);
    merged.push(name);
  }
  return merged;
}

function replaceFileAtomic(src, dst) {
  const tmp = join(dirname(dst), `.${basename(dst)}.tmp-${process.pid}`);
  copyFileSync(src, tmp);
  renameSync(tmp, dst);
}

function copyTreeAtomic(srcDir, dstDir) {
  mkdirSync(dstDir, { recursive: true });
  const names = readdirSync(srcDir).sort((a, b) => {
    if (a === "index.html") return 1;
    if (b === "index.html") return -1;
    return a.localeCompare(b);
  });
  for (const name of names) {
    const src = join(srcDir, name);
    const dst = join(dstDir, name);
    const srcStat = lstatSync(src);
    if (srcStat.isDirectory()) {
      if (existsSync(dst) && !lstatSync(dst).isDirectory()) {
        rmSync(dst, { recursive: true, force: true });
      }
      copyTreeAtomic(src, dst);
      continue;
    }
    if (!srcStat.isFile()) continue;
    if (existsSync(dst) && lstatSync(dst).isDirectory()) {
      rmSync(dst, { recursive: true, force: true });
    }
    replaceFileAtomic(src, dst);
  }
}

function pruneMissingEntries(srcDir, dstDir) {
  if (!existsSync(dstDir)) return;
  for (const name of readdirSync(dstDir)) {
    const src = join(srcDir, name);
    const dst = join(dstDir, name);
    if (!existsSync(src)) {
      rmSync(dst, { recursive: true, force: true });
      continue;
    }
    if (lstatSync(src).isDirectory() && lstatSync(dst).isDirectory()) {
      pruneMissingEntries(src, dst);
    }
  }
}

export function publishBuild(newDist, distDir, onBeforePrune = null) {
  const indexPath = join(newDist, "index.html");
  if (!existsSync(indexPath) || !lstatSync(indexPath).isFile()) {
    throw new Error("new frontend build is missing index.html");
  }
  const merged = mergePreviousAssets(distDir, newDist);
  mkdirSync(distDir, { recursive: true });
  copyTreeAtomic(newDist, distDir);
  if (onBeforePrune) onBeforePrune();
  pruneMissingEntries(newDist, distDir);
  return merged;
}

function build() {
  const distDir = process.env.VITE_OUT_DIR
    ? resolve(frontendDir, process.env.VITE_OUT_DIR)
    : join(frontendDir, "dist");
  const tmpDist = `${distDir}.building-${process.pid}`;
  rmSync(tmpDist, { recursive: true, force: true });

  const localBin = (pkg, bin) => join(frontendDir, "node_modules", pkg, "bin", bin);
  try {
    execFileSync(process.execPath, [localBin("typescript", "tsc"), "-b"], {
      cwd: frontendDir,
      stdio: "inherit",
    });
    execFileSync(process.execPath, [localBin("vite", "vite.js"), "build"], {
      cwd: frontendDir,
      stdio: "inherit",
      env: { ...process.env, VITE_OUT_DIR: tmpDist },
    });
    const merged = publishBuild(tmpDist, distDir);
    if (merged.length) {
      console.log(`[build-atomic] kept ${merged.length} previous hashed asset(s) for live tabs`);
    }
  } finally {
    rmSync(tmpDist, { recursive: true, force: true });
  }
}

const invokedDirectly =
  process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href;

if (invokedDirectly) {
  if (process.argv[2] === "--merge-assets") {
    const [prev, next] = process.argv.slice(3);
    if (!prev || !next) {
      console.error("usage: build-atomic.mjs --merge-assets <previousDist> <newDist>");
      process.exit(2);
    }
    const merged = mergePreviousAssets(resolve(prev), resolve(next));
    console.log(JSON.stringify({ merged }));
  } else if (process.argv[2] === "--publish-build") {
    const [next, dist] = process.argv.slice(3);
    if (!next || !dist) {
      console.error("usage: build-atomic.mjs --publish-build <newDist> <dist>");
      process.exit(2);
    }
    const sentinel = join(resolve(dist), ".publish-sentinel");
    writeFileSync(sentinel, "before", { flag: "wx" });
    let sentinelExistedDuringPublish = false;
    const merged = publishBuild(resolve(next), resolve(dist), () => {
      sentinelExistedDuringPublish = existsSync(sentinel);
    });
    console.log(JSON.stringify({
      merged,
      sentinelExistedDuringPublish,
    }));
    rmSync(sentinel, { force: true });
  } else {
    build();
  }
}
