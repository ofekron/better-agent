import { describe, it, expect } from "vitest";
import config from "../vite.config";

// Regression: with a relative base ('./'), Vite emits asset URLs like
// `./assets/index-*.js`. Those resolve against the current path, so a
// deep link such as `/s/<id>` requests `/s/assets/index-*.js`, which the
// backend's SPA 404 fallback answers with index.html (text/html). The
// browser then fails to execute the HTML as a module → white page.
// An absolute base ('/') makes asset URLs path-depth independent.
describe("vite base path", () => {
  it("is absolute so deep-link asset URLs resolve from the root", () => {
    const base = typeof config === "function" ? undefined : config.base;
    expect(base).toBe("/");
  });
});
