import { describe, expect, it } from "vitest";

import { mobileRightPanelSizingStyle } from "../src/utils/mobileRightPanelStyle";

describe("mobile right panel sizing", () => {
  it("pins the mobile drawer flex size to the resized height", () => {
    expect(mobileRightPanelSizingStyle(320)).toEqual({
      height: 320,
      minHeight: 320,
      maxHeight: 320,
      flex: "0 0 320px",
    });
  });
});
