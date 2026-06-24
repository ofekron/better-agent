import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { fileToPastedImage } from "../src/utils/imageAttach";

// happy-dom's canvas can't actually encode pixels, so stub the two
// pieces fileToPastedImage relies on: HTMLImageElement load (so the
// canvas branch runs at all) and canvas.toDataURL (the encode). This
// locks the dataUrl→base64/mediaType split logic, not jsdom's canvas.
describe("fileToPastedImage", () => {
  const FAKE_JPEG = "data:image/jpeg;base64,QUJD"; // base64("ABC")

  beforeEach(() => {
    vi.spyOn(HTMLCanvasElement.prototype, "toDataURL").mockReturnValue(FAKE_JPEG);
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({
      drawImage: () => {},
    } as unknown as CanvasRenderingContext2D);
    vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:fake");
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
    // Fire onload synchronously when `src` is assigned so the canvas
    // branch resolves without a real decode.
    Object.defineProperty(HTMLImageElement.prototype, "src", {
      configurable: true,
      set() {
        // width/height default to 0 → no resize path; fine for the test.
        this.onload?.(new Event("load"));
      },
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("encodes a blob to a {dataUrl, base64, mediaType} PastedImage", async () => {
    const blob = new Blob(["x"], { type: "image/png" });
    const result = await fileToPastedImage(blob);
    expect(result.mediaType).toBe("image/jpeg");
    expect(result.dataUrl).toBe(FAKE_JPEG);
    expect(result.base64).toBe("QUJD");
    expect(result.dataUrl.startsWith("data:")).toBe(true);
  });
});
