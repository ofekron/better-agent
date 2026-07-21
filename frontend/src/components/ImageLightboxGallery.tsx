import { useCallback, useEffect, useState, type ReactNode } from "react";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { useTranslation } from "react-i18next";
import { useBackButtonDismiss } from "../hooks/useBackButtonDismiss";
import Icon from "./Icon";

export interface LightboxImage {
  src: string;
  alt: string;
}

interface Props {
  images: LightboxImage[];
  children: (openImage: (index: number) => void) => ReactNode;
}

export function ImageLightboxGallery({ images, children }: Props) {
  const { t } = useTranslation();
  const prefersReducedMotion = useReducedMotion();
  const [selectedIndex, setSelectedIndex] = useState<number | null>(null);
  const close = useCallback(() => setSelectedIndex(null), []);
  useBackButtonDismiss(selectedIndex !== null, close);

  const navigate = useCallback((direction: 1 | -1) => {
    setSelectedIndex((current) => {
      if (current === null || images.length === 0) return null;
      return (current + direction + images.length) % images.length;
    });
  }, [images.length]);

  useEffect(() => {
    if (selectedIndex !== null && selectedIndex >= images.length) close();
  }, [close, images.length, selectedIndex]);

  useEffect(() => {
    if (selectedIndex === null) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") close();
      if (event.key === "ArrowLeft") navigate(-1);
      if (event.key === "ArrowRight") navigate(1);
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [close, navigate, selectedIndex]);

  const selectedImage = selectedIndex === null ? null : images[selectedIndex];

  return (
    <>
      {children(setSelectedIndex)}
      <AnimatePresence>
        {selectedImage && selectedIndex !== null && (
          <motion.div
            key="image-lightbox"
            className="image-lightbox-overlay"
            role="dialog"
            aria-modal="true"
            aria-label={selectedImage.alt}
            initial={{ opacity: prefersReducedMotion ? 1 : 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: prefersReducedMotion ? 1 : 0 }}
            transition={{ duration: prefersReducedMotion ? 0 : 0.18 }}
            onClick={close}
          >
            <motion.img
              src={selectedImage.src}
              alt={selectedImage.alt}
              className="image-lightbox-img"
              initial={{ opacity: prefersReducedMotion ? 1 : 0, scale: prefersReducedMotion ? 1 : 0.96 }}
              animate={{ opacity: 1, scale: 1 }}
              exit={{ opacity: prefersReducedMotion ? 1 : 0, scale: prefersReducedMotion ? 1 : 0.96 }}
              transition={{ duration: prefersReducedMotion ? 0 : 0.22, ease: [0.2, 0.8, 0.2, 1] }}
              onClick={(event) => event.stopPropagation()}
            />
            {images.length > 1 && (
              <>
                <button
                  type="button"
                  className="image-lightbox-nav image-lightbox-prev"
                  aria-label={t("imagePreview.previous")}
                  onClick={(event) => {
                    event.stopPropagation();
                    navigate(-1);
                  }}
                >
                  ‹
                </button>
                <button
                  type="button"
                  className="image-lightbox-nav image-lightbox-next"
                  aria-label={t("imagePreview.next")}
                  onClick={(event) => {
                    event.stopPropagation();
                    navigate(1);
                  }}
                >
                  ›
                </button>
              </>
            )}
            <button
              type="button"
              className="image-lightbox-close"
              aria-label={t("common.close")}
              onClick={close}
            >
              <Icon name="x" size={18} />
            </button>
            <div className="image-lightbox-counter" aria-live="polite">
              {selectedIndex + 1} / {images.length}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </>
  );
}
