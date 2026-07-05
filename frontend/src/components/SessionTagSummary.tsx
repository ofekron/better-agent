import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useBackButtonDismiss } from "../hooks/useBackButtonDismiss";
import type { SessionTag } from "../types";
import type { PopoverAnchor } from "./SessionTagPopover";

const TAG_SUMMARY_GAP = 4;

function tagChipStyle(tag: SessionTag) {
  return tag.color ? {
    background: `color-mix(in srgb, ${tag.color} 18%, transparent)`,
    borderColor: `color-mix(in srgb, ${tag.color} 55%, transparent)`,
  } : undefined;
}

export function SessionTagSummary({ tags }: { tags: SessionTag[] }) {
  const { t } = useTranslation();
  const containerRef = useRef<HTMLDivElement | null>(null);
  const measureChipRefs = useRef(new Map<string, HTMLSpanElement>());
  const measureCountRefs = useRef(new Map<number, HTMLButtonElement>());
  const [visibleCount, setVisibleCount] = useState(tags.length);
  const [moreAnchor, setMoreAnchor] = useState<PopoverAnchor | null>(null);

  const measure = useCallback(() => {
    const container = containerRef.current;
    if (!container) return;

    const style = window.getComputedStyle(container);
    const inlinePadding =
      Number.parseFloat(style.paddingInlineStart || style.paddingLeft || "0") +
      Number.parseFloat(style.paddingInlineEnd || style.paddingRight || "0");
    const available = Math.max(0, container.clientWidth - inlinePadding);
    const widths = tags.map((tag) =>
      measureChipRefs.current.get(tag.id)?.getBoundingClientRect().width ?? 0,
    );

    for (let candidate = tags.length; candidate >= 0; candidate -= 1) {
      const hidden = tags.length - candidate;
      const chipsWidth = widths
        .slice(0, candidate)
        .reduce((sum, width) => sum + width, 0);
      const chipGaps = Math.max(0, candidate - 1) * TAG_SUMMARY_GAP;
      const countWidth = hidden > 0
        ? measureCountRefs.current.get(hidden)?.getBoundingClientRect().width ?? 0
        : 0;
      const countGap = hidden > 0 && candidate > 0 ? TAG_SUMMARY_GAP : 0;

      if (chipsWidth + chipGaps + countWidth + countGap <= available) {
        setVisibleCount(candidate);
        return;
      }
    }

    setVisibleCount(0);
  }, [tags]);

  useLayoutEffect(() => {
    measure();
    const container = containerRef.current;
    if (!container) return;

    if (typeof ResizeObserver !== "undefined") {
      const observer = new ResizeObserver(measure);
      observer.observe(container);
      return () => observer.disconnect();
    }

    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, [measure]);

  useEffect(() => {
    if (moreAnchor && visibleCount >= tags.length) setMoreAnchor(null);
  }, [moreAnchor, tags.length, visibleCount]);

  const visibleTags = tags.slice(0, visibleCount);
  const hiddenTags = tags.slice(visibleCount);

  return (
    <>
      <div className="session-tags" ref={containerRef} onClick={(e) => e.stopPropagation()}>
        {visibleTags.map((tag) => (
          <span
            key={tag.id}
            className="session-tag-chip"
            title={tag.name}
            style={tagChipStyle(tag)}
          >
            {tag.name}
          </span>
        ))}
        {hiddenTags.length > 0 && (
          <button
            type="button"
            className="session-tag-count"
            title={hiddenTags.map((tag) => tag.name).join(", ")}
            aria-label={t("session.tagsMore", { count: hiddenTags.length })}
            onClick={(e) => {
              e.stopPropagation();
              const rect = e.currentTarget.getBoundingClientRect();
              setMoreAnchor({ top: rect.top, bottom: rect.bottom, left: rect.left });
            }}
          >
            {t("session.tagsMore", { count: hiddenTags.length })}
          </button>
        )}
        <div className="session-tags-measure" aria-hidden="true">
          {tags.map((tag) => (
            <span
              key={tag.id}
              ref={(el) => {
                if (el) measureChipRefs.current.set(tag.id, el);
                else measureChipRefs.current.delete(tag.id);
              }}
              className="session-tag-chip"
              style={tagChipStyle(tag)}
            >
              {tag.name}
            </span>
          ))}
          {tags.map((_, index) => {
            const count = index + 1;
            return (
              <button
                key={count}
                ref={(el) => {
                  if (el) measureCountRefs.current.set(count, el);
                  else measureCountRefs.current.delete(count);
                }}
                type="button"
                className="session-tag-count"
              >
                {t("session.tagsMore", { count })}
              </button>
            );
          })}
        </div>
      </div>
      {moreAnchor && hiddenTags.length > 0 && (
        <SessionTagMorePopover
          anchor={moreAnchor}
          tags={hiddenTags}
          onClose={() => setMoreAnchor(null)}
        />
      )}
    </>
  );
}

function SessionTagMorePopover({
  anchor,
  tags,
  onClose,
}: {
  anchor: PopoverAnchor;
  tags: SessionTag[];
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const ref = useRef<HTMLDivElement | null>(null);
  const [pos, setPos] = useState<React.CSSProperties>({
    position: "fixed",
    zIndex: 1000,
    top: anchor.bottom + 4,
    left: anchor.left,
  });

  useBackButtonDismiss(true, onClose);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    const margin = 8;
    const viewportWidth = window.innerWidth;
    const viewportHeight = window.innerHeight;
    let top: number | undefined = anchor.bottom + 4;
    let bottom: number | undefined;

    if (top + rect.height > viewportHeight - margin) {
      bottom = viewportHeight - anchor.top + 4;
      top = undefined;
    }

    let left = anchor.left;
    if (left + rect.width > viewportWidth - margin) left = viewportWidth - rect.width - margin;
    if (left < margin) left = margin;

    setPos({
      position: "fixed",
      zIndex: 1000,
      ...(bottom !== undefined ? { bottom } : { top }),
      left,
    });
  }, [anchor]);

  useEffect(() => {
    const onDocClick = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("mousedown", onDocClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDocClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [onClose]);

  return (
    <div
      ref={ref}
      className="session-tag-more-popover"
      role="dialog"
      aria-modal="false"
      aria-label={t("session.tags")}
      style={pos}
      onClick={(e) => e.stopPropagation()}
    >
      {tags.map((tag) => (
        <span
          key={tag.id}
          className="session-tag-chip"
          title={tag.name}
          style={tagChipStyle(tag)}
        >
          {tag.name}
        </span>
      ))}
    </div>
  );
}
