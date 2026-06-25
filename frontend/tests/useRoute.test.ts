import { ASK_SINGLETON_ID } from "../src/askSession";
import { extensionPanelPath, parseRoutePath } from "../src/hooks/useRoute";

describe("useRoute path parsing", () => {
  it("parses generic extension panel routes", () => {
    expect(parseRoutePath("/extensions/ofek-dev.routines/panels/output/routine-1")).toEqual({
      kind: "extensionPanel",
      extensionId: "ofek-dev.routines",
      panelId: "output",
      resourceId: "routine-1",
    });
  });

  it("falls back to Ask on malformed encoded extension panel routes", () => {
    expect(parseRoutePath("/extensions/ofek-dev.routines/panels/output/%")).toEqual({
      kind: "session",
      sessionId: ASK_SINGLETON_ID,
    });
  });

  it("builds extension panel paths with encoded segments", () => {
    expect(extensionPanelPath("ofek-dev.routines", "output", "routine/id")).toBe(
      "/extensions/ofek-dev.routines/panels/output/routine%2Fid",
    );
  });
});
