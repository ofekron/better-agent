import { useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { useTranslation } from "react-i18next";
import { useBackgroundWork } from "../hooks/useBackgroundWork";
import { BackgroundWorkItemCard } from "./BackgroundWorkItemCard";

/** The one place background work is shown.
 *
 * Every producer — core subsystem or granted extension — reports into the
 * backend registry, and this stack renders the result. It replaces the
 * per-subsystem popups that each owned their own fetch, dismissal rule and
 * fixed-position offset.
 *
 * Sits bottom-right, bottom-left under RTL. Above COLLAPSE_THRESHOLD rows it
 * collapses to a summary the user can expand, so a busy backend cannot bury
 * the app behind a wall of toasts.
 */

const COLLAPSE_THRESHOLD = 3;

export function BackgroundWorkManager() {
  const { t } = useTranslation();
  const { items, live, dismiss } = useBackgroundWork();
  const [expanded, setExpanded] = useState(false);

  if (items.length === 0) return null;

  const collapsed = items.length > COLLAPSE_THRESHOLD && !expanded;
  const shown = collapsed ? items.slice(0, COLLAPSE_THRESHOLD) : items;
  const activeCount = items.filter(
    (item) => item.status === "running" || item.status === "pending",
  ).length;

  return (
    <aside
      className="background-work-manager"
      role="status"
      aria-live="polite"
      aria-label={t("backgroundWork.title")}
    >
      <div className="background-work-header">
        <span className="background-work-title">
          {activeCount > 0
            ? t("backgroundWork.activeTitle", { count: activeCount })
            : t("backgroundWork.title")}
        </span>
        {!live ? (
          <span className="background-work-offline">
            {t("backgroundWork.offline")}
          </span>
        ) : null}
      </div>

      <ul className="background-work-list">
        <AnimatePresence initial={false}>
          {shown.map((item) => (
            <motion.div
              key={item.id}
              layout
              initial={{ opacity: 0, y: 12 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -8 }}
              transition={{ duration: 0.22, ease: "easeOut" }}
            >
              <BackgroundWorkItemCard
                item={item}
                stale={!live}
                onDismiss={dismiss}
              />
            </motion.div>
          ))}
        </AnimatePresence>
      </ul>

      {items.length > COLLAPSE_THRESHOLD ? (
        <button
          className="background-work-toggle"
          onClick={() => setExpanded((prev) => !prev)}
        >
          {collapsed
            ? t("backgroundWork.showAll", { count: items.length - COLLAPSE_THRESHOLD })
            : t("backgroundWork.showLess")}
        </button>
      ) : null}
    </aside>
  );
}
