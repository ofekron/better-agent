#!/usr/bin/env node
import { createHash } from "node:crypto";
import { existsSync, mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { syncMarketingDesktopDownload } from "./sync-marketing-desktop-download.mjs";

function fail(message) {
  throw new Error(message);
}

function write(path, contents) {
  writeFileSync(path, contents);
}

const repo = mkdtempSync(join(tmpdir(), "bc-marketing-sync-"));

try {
  mkdirSync(join(repo, "desktop", "dist"), { recursive: true });
  mkdirSync(join(repo, "marketing", "better-agent"), { recursive: true });
  write(join(repo, "desktop", "_version.py"), '__version__ = "0.1.42"\n');
  write(join(repo, "desktop", "dist", "BetterAgent.dmg"), "fresh-dmg");
  write(
    join(repo, "marketing", "better-agent", "index.html"),
    [
      '<a href="./downloads/BetterAgent-macOS-arm64.dmg?v=0.1.1">Hero</a>',
      '<a href="./downloads/BetterAgent-macOS-arm64.dmg?v=0.1.1">Download</a>',
    ].join("\n"),
  );

  const result = syncMarketingDesktopDownload(repo);
  const expectedHash = createHash("sha256").update("fresh-dmg").digest("hex");
  if (result.version !== "0.1.42") fail(`expected version 0.1.42, got ${result.version}`);
  if (result.sha256 !== expectedHash) fail(`expected hash ${expectedHash}, got ${result.sha256}`);

  const dmg = readFileSync(join(repo, "marketing", "better-agent", "downloads", "BetterAgent-macOS-arm64.dmg"), "utf8");
  if (dmg !== "fresh-dmg") fail("expected marketing DMG to be replaced");

  const checksums = readFileSync(join(repo, "marketing", "better-agent", "downloads", "SHA256SUMS.txt"), "utf8");
  if (checksums !== `${expectedHash}  BetterAgent-macOS-arm64.dmg\n`) {
    fail(`unexpected checksum file: ${checksums}`);
  }

  const index = readFileSync(join(repo, "marketing", "better-agent", "index.html"), "utf8");
  const expectedVersionRefs = index.match(/BetterAgent-macOS-arm64\.dmg\?v=0\.1\.42/g) || [];
  if (expectedVersionRefs.length !== 2) {
    fail(`expected both index links to use 0.1.42, got ${index}`);
  }

  rmSync(join(repo, "desktop", "dist", "BetterAgent.dmg"), { force: true });
  const skipped = syncMarketingDesktopDownload(repo);
  if (skipped.skipped !== true) fail("expected missing source DMG to skip");
  if (skipped.version !== "0.1.42") fail(`expected skipped version 0.1.42, got ${skipped.version}`);

  write(join(repo, "desktop", "dist", "BetterAgent.dmg"), "fresh-dmg");
  rmSync(join(repo, "marketing", "better-agent", "index.html"), { force: true });
  rmSync(join(repo, "marketing", "better-agent", "downloads"), { recursive: true, force: true });
  const missingIndex = syncMarketingDesktopDownload(repo);
  if (missingIndex.skipped !== true) fail("expected missing marketing index to skip");
  if (missingIndex.reason !== "marketing/better-agent/index.html is missing") {
    fail(`unexpected missing index reason: ${missingIndex.reason}`);
  }
  if (existsSync(join(repo, "marketing", "better-agent", "downloads", "BetterAgent-macOS-arm64.dmg"))) {
    fail("expected missing index skip to avoid copying the DMG");
  }
} finally {
  rmSync(repo, { recursive: true, force: true });
}
