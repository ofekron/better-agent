const WEB_RUNTIME_INPUTS = [
  /^frontend\/src\//,
  /^frontend\/public\//,
  /^frontend\/index\.html$/,
  /^frontend\/vite\.config\.[^/]+$/,
  /^frontend\/tsconfig(?:\.[^/]+)?\.json$/,
  /^frontend\/package\.json$/,
  /^frontend\/(?:package-lock|pnpm-lock)\.yaml$|^frontend\/package-lock\.json$/,
  /^frontend\/pnpm-workspace\.yaml$/,
  /^frontend\/patches\//,
];

const ANDROID_NATIVE_INPUTS = [
  /^frontend\/capacitor\.config\.[^/]+$/,
  /^frontend\/android\/app\/src\/main\//,
  /^frontend\/android\/app\/(?:build\.gradle(?:\.kts)?|capacitor\.build\.gradle|proguard-rules\.pro)$/,
  /^frontend\/android\/(?:build\.gradle(?:\.kts)?|settings\.gradle(?:\.kts)?|gradle\.properties|variables\.gradle|capacitor\.settings\.gradle)$/,
  /^frontend\/android\/gradle\/wrapper\/(?:gradle-wrapper\.jar|gradle-wrapper\.properties)$/,
  /^frontend\/android\/gradlew(?:\.bat)?$/,
  /^frontend\/android\/capacitor-cordova-android-plugins\/(?:build\.gradle|cordova\.variables\.gradle|src\/main\/)/,
];

export function androidRebuildReason(path) {
  if (WEB_RUNTIME_INPUTS.some((pattern) => pattern.test(path))) {
    return "web-runtime";
  }
  if (ANDROID_NATIVE_INPUTS.some((pattern) => pattern.test(path))) {
    return "android-native";
  }
  return null;
}

export function androidRebuildDecision(paths) {
  const relevantPaths = paths.filter((path) => androidRebuildReason(path) !== null);
  return {
    rebuild: relevantPaths.length > 0,
    relevantPaths,
  };
}
