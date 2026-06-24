import type { FileAttachment } from "../types";

const MAX_FILE_SIZE = 10 * 1024 * 1024; // 10 MB

/** Read a non-image File as a base64 FileAttachment.
 *  Rejects files exceeding 10 MB. */
export function fileToAttachment(file: File): Promise<FileAttachment> {
  return new Promise((resolve, reject) => {
    if (file.size > MAX_FILE_SIZE) {
      reject(new Error(`File "${file.name}" exceeds 10 MB limit`));
      return;
    }
    const reader = new FileReader();
    reader.onload = (e) => {
      const dataUrl = e.target?.result as string;
      const [header, base64] = dataUrl.split(",");
      const mediaType = header.match(/data:([^;]+)/)?.[1] || "application/octet-stream";
      resolve({ name: file.name, base64, mediaType, size: file.size });
    };
    reader.onerror = () => reject(reader.error ?? new Error("read failed"));
    reader.readAsDataURL(file);
  });
}
