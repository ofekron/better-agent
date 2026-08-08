// Shared attachment renderer for any node payload carrying
// `AttachmentWire[]` — currently `TypedPromptPayloadWire.attachments`
// (TypedPromptView) and `SteeringMessagePayloadWire.attachments`
// (SteeringMessageView, ContentLeaves.tsx). Both payload shapes carry the
// exact same `Attachment` model (backend/surface_contract/nodes.py), so
// this is the ONE renderer for it — single implementation, no fork.
//
// At parity with legacy MessageBubble.tsx's `UserImages`/`UserFiles` split:
// an image attachment (`media_type` starting with "image/") renders as a
// real `<img class="message-image">` thumbnail inside a click-to-open
// `ImageLightboxGallery` (legacy's `.message-images`/`.message-image-open`
// CSS, reused verbatim); every other attachment renders as a
// `.message-file-badge` name+size chip (legacy's `UserFiles`). Unlike
// legacy's blob-fetch (`buildMessageImageUrl` + `fetch` + `URL.createObjectURL`,
// needed because the legacy REST route requires bearer-token auth outside
// same-origin cookie contexts), the native `adapter/client.ts#attachmentUrl`
// route is same-origin-cookie-authed — a plain `<img src>` URL, no fetch
// needed, no object-URL lifecycle to manage.
//
// `ImageLightboxGallery` is lazy-imported (same rationale as
// `ContentLeaves.tsx`'s `MessageBox`/`ThinkingBlock`): it pulls in
// framer-motion, which a plain filename/thumbnail render shouldn't force
// into this leaf's eager bundle just because SOME attachment might be an
// image.

import { lazy, Suspense, useMemo } from "react";
import { useTranslation } from "react-i18next";
import { attachmentUrl } from "../../adapter/client";
import type { AttachmentWire } from "../../adapter/wire";
import { formatAttachmentSize } from "../../utils/attachmentSize";

const ImageLightboxGallery = lazy(() =>
  import("../../components/ImageLightboxGallery").then((m) => ({ default: m.ImageLightboxGallery })),
);

function isImageAttachment(a: AttachmentWire): boolean {
  return a.media_type.startsWith("image/");
}

export function AttachmentChips({ attachments, sessionId }: { attachments: AttachmentWire[]; sessionId: string }) {
  const { t } = useTranslation();
  const images = useMemo(() => attachments.filter(isImageAttachment), [attachments]);
  const files = useMemo(() => attachments.filter((a) => !isImageAttachment(a)), [attachments]);

  if (attachments.length === 0) return null;

  return (
    <>
      {images.length > 0 && (
        <Suspense fallback={null}>
          <ImageLightboxGallery
            images={images.map((a, index) => ({
              src: attachmentUrl(sessionId, a.ref),
              alt: t("input.attachedImageAlt", { index: index + 1 }),
            }))}
          >
            {(openImage) => (
              <div className="message-images">
                {images.map((a, i) => (
                  <button
                    key={a.ref}
                    type="button"
                    className="message-image-open"
                    aria-label={t("input.attachedImageAlt", { index: i + 1 })}
                    onClick={() => openImage(i)}
                  >
                    <img src={attachmentUrl(sessionId, a.ref)} alt="" className="message-image" />
                  </button>
                ))}
              </div>
            )}
          </ImageLightboxGallery>
        </Suspense>
      )}
      {files.length > 0 && (
        <div className="message-files">
          {files.map((a) => (
            <div key={a.ref} className="message-file-badge">
              <span className="message-file-name">{a.name}</span>
              {a.size !== null && <span className="message-file-size">{formatAttachmentSize(a.size)}</span>}
            </div>
          ))}
        </div>
      )}
    </>
  );
}
