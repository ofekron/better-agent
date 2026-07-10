// Line Switch quick button: shows the active line (dev/main) and switches the
// running backend+frontend to the other worktree. Truthful states only: the
// pointer/status come from the backend; while the backend restarts we show an
// explicit indeterminate "switching" state and poll until the new build is up,
// then hard-reload so the page matches the new line's bundle.

const EXT = "ofek-dev.switch-control";
const POLL_MS = 2000;

function apiUrl(context, path) {
  return `${context.apiBaseUrl || ""}${path}`;
}

async function fetchState(context) {
  const response = await fetch(
    apiUrl(context, `/api/extensions/${EXT}/backend/state`),
    { credentials: "include" },
  );
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}

const stateStores = new Map();
function stateStore(apiBase) {
  if (stateStores.has(apiBase)) return stateStores.get(apiBase);
  const store = { value: null, inFlight: null, listeners: new Set(), timer: null, lastAttempt: 0 };
  stateStores.set(apiBase, store);
  return store;
}
function refreshState(apiBase, store) {
  if (document.hidden || Date.now() - store.lastAttempt < 30000) return store.inFlight || Promise.resolve(false);
  if (store.inFlight) return store.inFlight;
  store.lastAttempt = Date.now();
  store.inFlight = fetchState({ apiBaseUrl: apiBase }).then((value) => {
    store.value = value;
    for (const listener of store.listeners) listener(value);
    return true;
  }).catch(() => false).finally(() => { store.inFlight = null; });
  return store.inFlight;
}
function subscribeState(apiBase, listener) {
  const store = stateStore(apiBase);
  store.listeners.add(listener);
  if (store.value) listener(store.value);
  if (store.listeners.size === 1) store.timer = setInterval(() => void refreshState(apiBase, store), 30000);
  void refreshState(apiBase, store);
  return () => {
    store.listeners.delete(listener);
    if (!store.listeners.size && store.timer) clearInterval(store.timer);
  };
}
export const switchStateTestApi = { stateStore, refreshState, subscribeState, reset() { stateStores.clear(); } };

export function Component({ context, React }) {
  const { useState, useEffect, useCallback, useRef } = React;
  const t = typeof context.t === "function" ? context.t : (_key, fallback) => fallback;
  const [state, setState] = useState(null);
  const [open, setOpen] = useState(false);
  const [switching, setSwitching] = useState(false);
  const [error, setError] = useState("");
  const pollRef = useRef(null);
  const apiBase = context.apiBaseUrl || "";

  const refresh = useCallback(async () => {
    try {
      await refreshState(apiBase, stateStore(apiBase));
    } catch {
      /* backend unreachable: keep the last truthful state */
    }
  }, [apiBase]);

  useEffect(() => {
    return subscribeState(apiBase, setState);
  }, [apiBase]);

  useEffect(() => {
    const events = Array.isArray(context.events) ? context.events : [];
    const last = events[events.length - 1];
    if (!last || last.type !== "switch_control_state_changed" || !last.state) return;
    const store = stateStore(apiBase);
    store.value = last.state;
    for (const listener of store.listeners) listener(last.state);
  }, [apiBase, context.events]);

  const waitForNewLine = useCallback(
    (requestId) => {
      if (pollRef.current) clearInterval(pollRef.current);
      pollRef.current = setInterval(async () => {
        try {
          const response = await fetch(
            apiUrl(context, `/api/admin/restart-status/${encodeURIComponent(requestId)}`),
            { credentials: "include" },
          );
          if (!response.ok) return;
          const payload = await response.json();
          if (payload && payload.status === "succeeded") {
            clearInterval(pollRef.current);
            window.location.reload();
          }
          if (payload && payload.status === "failed") {
            clearInterval(pollRef.current);
            setSwitching(false);
            setError(t("switchControl.buildFailed", "Frontend build failed — still on the previous line"));
            void refresh();
          }
        } catch {
          /* backend down mid-switch: keep polling */
        }
      }, POLL_MS);
      return () => clearInterval(pollRef.current);
    },
    [context, refresh, t],
  );

  useEffect(() => () => pollRef.current && clearInterval(pollRef.current), []);

  const doSwitch = useCallback(
    async (target) => {
      setOpen(false);
      setSwitching(true);
      setError("");
      try {
        const response = await fetch(apiUrl(context, `/api/extensions/${EXT}/backend/switch`), {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ target }),
        });
        if (!response.ok) throw new Error(await response.text());
        const payload = await response.json();
        waitForNewLine(payload.request_id);
      } catch (e) {
        setSwitching(false);
        setError(e instanceof Error ? e.message : String(e));
        void refresh();
      }
    },
    [context, refresh, waitForNewLine],
  );

  if (!state || !state.switchable) return null;
  const active = state.active_line || "?";
  const pointerStatus = (state.pointer && state.pointer.status) || "";
  const label = switching
    ? t("switchControl.switching", "Switching…")
    : `${t("switchControl.line", "Line")}: ${active}`;

  const children = [
    React.createElement(
      "button",
      {
        key: "btn",
        type: "button",
        className: "setup-btn switch-control-btn",
        disabled: switching,
        title: t("switchControl.tooltip", "Switch the running app between the main and dev lines"),
        "aria-label": label,
        "aria-busy": switching,
        onClick: () => setOpen((value) => !value),
      },
      switching ? React.createElement("span", { className: "switch-control-spinner" }, "⟳ ") : null,
      label,
    ),
  ];

  if (open && !switching) {
    children.push(
      React.createElement(
        "div",
        { key: "menu", className: "switch-control-menu", role: "menu" },
        Object.keys(state.lines).map((line) => {
          const isActive = line === active;
          const missing = (state.incompatible && state.incompatible[line]) || null;
          const blocked = Boolean(missing);
          const needsUpdate = t("switchControl.needsUpdate", "needs update");
          return React.createElement(
            "button",
            {
              key: line,
              type: "button",
              role: "menuitem",
              className:
                "switch-control-item" +
                (isActive ? " active" : "") +
                (blocked ? " incompatible" : ""),
              disabled: isActive || blocked,
              title: blocked ? `${needsUpdate}: ${missing.join(", ")}` : undefined,
              onClick: () => void doSwitch(line),
            },
            isActive
              ? `${line} — ${t("switchControl.active", "active")}`
              : blocked
                ? `${line} — ${needsUpdate}`
                : `${t("switchControl.switchTo", "Switch to")} ${line}`,
          );
        }),
        pointerStatus === "reverted"
          ? React.createElement(
              "div",
              { key: "reverted", className: "switch-control-note" },
              t("switchControl.reverted", "Last switch failed and was reverted"),
            )
          : null,
      ),
    );
  }
  if (error) {
    children.push(
      React.createElement("div", { key: "err", className: "switch-control-error", role: "alert" }, error),
    );
  }
  return React.createElement("span", { className: "switch-control" }, children);
}
