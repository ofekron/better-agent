import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { loadConfigFromFile, type ConfigEnv, type UserConfig } from "vite";
import { WEB_PLATFORM_ALIAS_TARGETS } from "../vite.web-platform-aliases";

const frontendRoot = path.resolve(__dirname, "..");

async function loadConfig(mode: string): Promise<UserConfig> {
  const env: ConfigEnv = { command: "build", mode };
  const loaded = await loadConfigFromFile(
    env,
    path.join(frontendRoot, "vite.config.ts"),
    frontendRoot,
  );
  if (!loaded) throw new Error(`Vite did not load its ${mode} config`);
  const config = loaded.config;
  return typeof config === "function" ? config(env) : config;
}

// Regression: with a relative base ('./'), Vite emits asset URLs like
// `./assets/index-*.js`. Those resolve against the current path, so a
// deep link such as `/s/<id>` requests `/s/assets/index-*.js`, which the
// backend's SPA 404 fallback answers with index.html (text/html). The
// browser then fails to execute the HTML as a module → white page.
// An absolute base ('/') makes asset URLs path-depth independent.
describe("vite base path", () => {
  it("uses root-relative assets in web and mobile builds", async () => {
    await expect(loadConfig("web")).resolves.toMatchObject({ base: "/" });
    await expect(loadConfig("mobile")).resolves.toMatchObject({ base: "/" });
  });

  it("uses one complete web-shim mapping and omits it from mobile builds", async () => {
    const web = await loadConfig("web");
    const mobile = await loadConfig("mobile");
    const webAliases = web.resolve?.alias as Record<string, string>;
    const mobileAliases = mobile.resolve?.alias as Record<string, string>;

    for (const moduleName of Object.keys(WEB_PLATFORM_ALIAS_TARGETS)) {
      const target = webAliases[moduleName];
      expect(path.isAbsolute(target), moduleName).toBe(true);
      expect(fs.existsSync(target), target).toBe(true);
      expect(mobileAliases[moduleName], moduleName).toBeUndefined();
    }
  });
});
