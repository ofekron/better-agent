#!/usr/bin/env node
import { androidRebuildDecision, androidRebuildReason } from "./android-rebuild-policy.mjs";

function expectReason(path, expected) {
  const actual = androidRebuildReason(path);
  if (actual !== expected) {
    throw new Error(`expected ${path} to resolve to ${expected}, got ${actual}`);
  }
}

for (const path of [
  "frontend/src/App.tsx",
  "frontend/src/i18n/en.json",
  "frontend/src/components/requirements-FileViewer.tsx.md",
  "frontend/public/icon.svg",
  "frontend/index.html",
  "frontend/vite.config.ts",
  "frontend/tsconfig.app.json",
  "frontend/package.json",
  "frontend/package-lock.json",
  "frontend/pnpm-lock.yaml",
  "frontend/pnpm-workspace.yaml",
  "frontend/patches/send-intent+7.0.0.patch",
]) {
  expectReason(path, "web-runtime");
}

for (const path of [
  "frontend/capacitor.config.ts",
  "frontend/android/app/src/main/AndroidManifest.xml",
  "frontend/android/app/src/main/java/com/betteragent/app/MainActivity.java",
  "frontend/android/app/build.gradle",
  "frontend/android/app/capacitor.build.gradle",
  "frontend/android/app/proguard-rules.pro",
  "frontend/android/build.gradle",
  "frontend/android/settings.gradle",
  "frontend/android/gradle.properties",
  "frontend/android/variables.gradle",
  "frontend/android/capacitor.settings.gradle",
  "frontend/android/gradle/wrapper/gradle-wrapper.jar",
  "frontend/android/gradlew.bat",
  "frontend/android/capacitor-cordova-android-plugins/src/main/AndroidManifest.xml",
]) {
  expectReason(path, "android-native");
}

for (const path of [
  "backend/main.py",
  "frontend/README.md",
  "frontend/tests/mobile.test.tsx",
  "frontend/eslint.config.js",
  "frontend/ios/App/App/AppDelegate.swift",
  "frontend/android/app/src/test/java/ExampleUnitTest.java",
  "frontend/android/app/src/androidTest/java/ExampleInstrumentedTest.java",
  "frontend/android/app/build/outputs/apk/debug/app-debug.apk",
  "frontend/android/.gradle/cache.bin",
]) {
  expectReason(path, null);
}

const decision = androidRebuildDecision([
  "backend/main.py",
  "frontend/src/App.tsx",
  "frontend/android/settings.gradle",
]);
if (!decision.rebuild) throw new Error("expected mixed inputs to require an Android rebuild");
if (decision.relevantPaths.join(",") !== "frontend/src/App.tsx,frontend/android/settings.gradle") {
  throw new Error(`unexpected relevant paths: ${decision.relevantPaths.join(",")}`);
}

if (androidRebuildDecision(["backend/main.py"]).rebuild) {
  throw new Error("expected backend-only inputs to skip the Android rebuild");
}

console.log("android rebuild policy tests passed");
