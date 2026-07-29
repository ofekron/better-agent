import { describe, expect, it } from "vitest";
import { ASK_SINGLETON_ID } from "../src/askSession";
import {
  firstAutoSelectableSession,
  isAutoSelectableSession,
} from "../src/autoSelectSession";
import { editSingletonId } from "../src/projectStructureEditSession";
import { makeSession } from "./fixtures";

describe("automatic session selection", () => {
  it("never redirects an Ask or Edit singleton route to itself", () => {
    const ask = makeSession({ id: ASK_SINGLETON_ID });
    const edit = makeSession({ id: editSingletonId() });

    expect(isAutoSelectableSession(ask)).toBe(false);
    expect(isAutoSelectableSession(edit)).toBe(false);
    expect(firstAutoSelectableSession([ask, edit])).toBeNull();
  });

  it("selects the first normal non-archived session", () => {
    const archived = makeSession({ id: "archived", archived: true });
    const expected = makeSession({ id: "normal" });

    expect(
      firstAutoSelectableSession([
        makeSession({ id: ASK_SINGLETON_ID }),
        archived,
        expected,
      ]),
    ).toBe(expected);
  });
});
