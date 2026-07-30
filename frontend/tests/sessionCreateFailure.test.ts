import { describe, expect, it } from "vitest";

import { HttpStatusError } from "../src/utils/offlineRequest";
import { planSessionCreateFailure } from "../src/utils/sessionCreateFailure";

const TIMEOUT_MSG = "timed out";

describe("planSessionCreateFailure", () => {
  it("never routes a file-edit create failure to the offline queue", () => {
    const abort = new DOMException("The operation was aborted.", "AbortError");
    expect(
      planSessionCreateFailure(abort, { fileEditEnabled: true, timeoutMessage: TIMEOUT_MSG }),
    ).toEqual({ kind: "alert", message: TIMEOUT_MSG });

    expect(
      planSessionCreateFailure(new HttpStatusError(500, "backend exploded"), {
        fileEditEnabled: true,
        timeoutMessage: TIMEOUT_MSG,
      }),
    ).toEqual({ kind: "alert", message: "backend exploded" });

    expect(
      planSessionCreateFailure(new TypeError("Failed to fetch"), {
        fileEditEnabled: true,
        timeoutMessage: TIMEOUT_MSG,
      }),
    ).toEqual({ kind: "alert", message: "Failed to fetch" });
  });

  it("queues non-file-edit transient failures offline", () => {
    const abort = new DOMException("The operation was aborted.", "AbortError");
    for (const error of [abort, new TypeError("Failed to fetch"), new HttpStatusError(503, "unavailable")]) {
      expect(
        planSessionCreateFailure(error, { fileEditEnabled: false, timeoutMessage: TIMEOUT_MSG }),
      ).toEqual({ kind: "queue-offline" });
    }
  });

  it("alerts non-retryable non-file-edit failures with the real message", () => {
    expect(
      planSessionCreateFailure(new HttpStatusError(422, "bad cwd"), {
        fileEditEnabled: false,
        timeoutMessage: TIMEOUT_MSG,
      }),
    ).toEqual({ kind: "alert", message: "bad cwd" });
  });
});
