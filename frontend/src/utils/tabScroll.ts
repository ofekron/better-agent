export function horizontalScrollTarget(
  container: HTMLElement,
  item: HTMLElement,
): number | null {
  const containerRect = container.getBoundingClientRect();
  const itemRect = item.getBoundingClientRect();
  const currentLeft = container.scrollLeft;
  const viewLeft = currentLeft;
  const viewRight = currentLeft + container.clientWidth;
  const itemLeft = currentLeft + itemRect.left - containerRect.left;
  const itemRight = currentLeft + itemRect.right - containerRect.left;
  const maxLeft = Math.max(0, container.scrollWidth - container.clientWidth);

  if (itemLeft < viewLeft) return Math.max(0, Math.min(itemLeft, maxLeft));
  if (itemRight <= viewRight) return null;

  const nextLeft =
    itemRect.width > container.clientWidth
      ? itemLeft
      : itemRight - container.clientWidth;
  return Math.max(0, Math.min(nextLeft, maxLeft));
}

export function horizontalCenterScrollTarget(
  container: HTMLElement,
  item: HTMLElement,
): number | null {
  const containerRect = container.getBoundingClientRect();
  const itemRect = item.getBoundingClientRect();
  const currentLeft = container.scrollLeft;
  const itemLeft = currentLeft + itemRect.left - containerRect.left;
  const itemCenter = itemLeft + itemRect.width / 2;
  const maxLeft = Math.max(0, container.scrollWidth - container.clientWidth);
  const nextLeft = Math.max(
    0,
    Math.min(itemCenter - container.clientWidth / 2, maxLeft),
  );

  return nextLeft === currentLeft ? null : nextLeft;
}

function scrollHorizontalItem(
  container: HTMLElement | null,
  item: HTMLElement | null,
  target: (container: HTMLElement, item: HTMLElement) => number | null,
) {
  if (!container || !item) return;

  const left = target(container, item);
  if (left === null) return;

  const reduceMotion =
    typeof window !== "undefined"
    && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  container.scrollTo({
    left,
    behavior: reduceMotion ? "auto" : "smooth",
  });
}

export function scrollHorizontalItemIntoView(
  container: HTMLElement | null,
  item: HTMLElement | null,
) {
  scrollHorizontalItem(container, item, horizontalScrollTarget);
}

export function scrollHorizontalItemToCenter(
  container: HTMLElement | null,
  item: HTMLElement | null,
) {
  scrollHorizontalItem(container, item, horizontalCenterScrollTarget);
}
