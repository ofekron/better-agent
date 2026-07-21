import { useTranslation } from "react-i18next";
import type { PastedImage } from "../types";
import { ImageLightboxGallery } from "./ImageLightboxGallery";

interface Props {
  images: PastedImage[];
  onRemove: (index: number) => void;
  className?: string;
}

export function ComposerImagePreviews({ images, onRemove, className }: Props) {
  const { t } = useTranslation();

  if (images.length === 0) return null;

  return (
    <ImageLightboxGallery
      images={images.map((image, index) => ({
        src: image.dataUrl,
        alt: t("input.attachedImageAlt", { index: index + 1 }),
      }))}
    >
      {(openImage) => (
        <div className={`image-previews${className ? ` ${className}` : ""}`}>
          {images.map((image, index) => {
            const alt = t("input.attachedImageAlt", { index: index + 1 });
            return (
              <div key={`${image.dataUrl}-${index}`} className="image-preview-item">
                <button
                  type="button"
                  className="image-preview-open"
                  aria-label={alt}
                  onClick={() => openImage(index)}
                >
                  <img src={image.dataUrl} alt="" />
                </button>
                <button
                  type="button"
                  className="image-remove-btn"
                  onClick={() => onRemove(index)}
                  title={t("input.removeImageTitle")}
                >
                  ×
                </button>
              </div>
            );
          })}
        </div>
      )}
    </ImageLightboxGallery>
  );
}
