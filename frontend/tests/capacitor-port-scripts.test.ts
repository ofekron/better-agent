import { describe, expect, it } from "vitest";
import pkg from "../package.json";

describe("Capacitor port scripts", () => {
  it("do not hardcode the old backend port", () => {
    const scripts = pkg.scripts;
    for (const name of ["cap:dev:ios", "cap:dev:android", "cap:prod:ios", "cap:prod:android"]) {
      expect(scripts[name]).not.toContain(":8000");
      expect(scripts[name]).toContain("18765");
    }
  });
});
