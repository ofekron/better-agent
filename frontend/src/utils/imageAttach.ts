import type { PastedImage } from "../types";

/** Max pixel dimension for an attached image. Phone cameras/screenshots
 *  produce 3-5MB files; resizing keeps WebSocket payloads small. */
const MAX_DIM = 1920;
const QUALITY = 0.8;

export function imageFilesFromClipboard(data: DataTransfer | null): File[] {
  if (!data) return [];
  return Array.from(data.items)
    .filter((item) => item.kind === "file" && item.type.startsWith("image/"))
    .map((item) => item.getAsFile())
    .filter((f): f is File => !!f);
}

/** Read a Blob/File as a base64 data URL (no resize). Used as the
 *  fallback when canvas-based resize fails (e.g. unsupported codec). */
function readAsPastedImage(file: Blob): Promise<PastedImage> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = (e) => {
      const dataUrl = e.target?.result as string;
      const [header, base64] = dataUrl.split(",");
      const mediaType = header.match(/data:([^;]+)/)?.[1] || "image/png";
      resolve({ dataUrl, base64, mediaType });
    };
    reader.onerror = () => reject(reader.error ?? new Error("read failed"));
    reader.readAsDataURL(file);
  });
}

/** Convert an image Blob/File into a {dataUrl, base64, mediaType}
 *  PastedImage, resizing to {@link MAX_DIM} and re-encoding as JPEG to
 *  cap payload size. Falls back to the original bytes if canvas resize
 *  fails. Single source of truth for both composer attachments and
 *  OS-share-sheet ingestion. */
export function fileToPastedImage(file: Blob): Promise<PastedImage> {
  return new Promise((resolve) => {
    const img = new Image();
    const objectUrl = URL.createObjectURL(file);
    img.onload = () => {
      URL.revokeObjectURL(objectUrl);
      let { width, height } = img;
      if (width > MAX_DIM || height > MAX_DIM) {
        const scale = MAX_DIM / Math.max(width, height);
        width = Math.round(width * scale);
        height = Math.round(height * scale);
      }
      const canvas = document.createElement("canvas");
      canvas.width = width;
      canvas.height = height;
      const ctx = canvas.getContext("2d")!;
      ctx.drawImage(img, 0, 0, width, height);
      const dataUrl = canvas.toDataURL("image/jpeg", QUALITY);
      const [, base64] = dataUrl.split(",");
      resolve({ dataUrl, base64, mediaType: "image/jpeg" });
    };
    img.onerror = () => {
      URL.revokeObjectURL(objectUrl);
      resolve(readAsPastedImage(file));
    };
    img.src = objectUrl;
  });
}
