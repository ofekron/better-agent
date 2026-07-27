import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import pkg from "../package.json";

describe("Capacitor port scripts", () => {
  it("do not hardcode the old backend port", () => {
    const scripts = pkg.scripts;
    for (const name of ["cap:dev:ios", "cap:dev:android", "cap:prod:ios", "cap:prod:android"]) {
      expect(scripts[name]).not.toContain(":8000");
      expect(scripts[name]).toContain("18765");
    }
  });

  it("does not proxy the web app to the old backend port", () => {
    const viteConfig = readFileSync("vite.config.ts", "utf8");
    expect(viteConfig).not.toContain("localhost:8000");
    expect(viteConfig).toContain("BETTER_AGENT_BACKEND_PORT");
    expect(viteConfig).toContain("18765");
  });
});
