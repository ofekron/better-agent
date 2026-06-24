#!/usr/bin/env node
import { createHash } from "node:crypto";
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
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
  mkdirSync(join(repo, "marketing", "better-agent", "downloads"), { recursive: true });
  write(join(repo, "desktop", "_version.py"), '__version__ = "0.1.42"\n');
  write(join(repo, "desktop", "dist", "BetterAgent.dmg"), "fresh-dmg");
  write(join(repo, "marketing", "better-agent", "downloads", "BetterAgent-macOS-arm64.dmg"), "stale-dmg");
  write(join(repo, "marketing", "better-agent", "downloads", "SHA256SUMS.txt"), "stale  BetterAgent-macOS-arm64.dmg\n");
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
} finally {
  rmSync(repo, { recursive: true, force: true });
}
