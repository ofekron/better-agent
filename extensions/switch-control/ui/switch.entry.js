// Line Switch quick button: shows the active line and moves the UI to the
// selected line. Parallel line instances navigate by port; legacy switches
// poll restart status until the selected checkout is live.

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

export function restartStatusForRequest(payload, requestId) {
  if (!payload || payload.request_id !== requestId) return { status: "pending", error: "" };
  const status = payload.status === "succeeded" || payload.status === "failed"
    ? payload.status
    : "pending";
  return { status, error: typeof payload.error === "string" ? payload.error : "" };
}

export function activeSwitchRequest(state) {
  const request = state && state.request;
  if (!request || typeof request.request_id !== "string") return null;
  return ["preparing", "pending", "accepted"].includes(request.status) ? request : null;
}

export function redirectUrlForLine(state, target, currentHref) {
  const lineTarget = state && state.line_targets && state.line_targets[target];
  const port = lineTarget && Number(lineTarget.backend_port);
  if (!Number.isInteger(port) || port < 1 || port > 65535) return "";
  const url = new URL(currentHref);
  url.port = String(port);
  return url.toString();
}

export function switchTargetUrl(payload) {
  return payload && typeof payload.target_url === "string" && payload.target_url
    ? payload.target_url
    : "";
}

export function Component({ context, React }) {
  const { useState, useEffect, useCallback, useRef } = React;
  const t = typeof context.t === "function" ? context.t : (_key, fallback) => fallback;
  const [state, setState] = useState(null);
  const [open, setOpen] = useState(false);
  const [switching, setSwitching] = useState(false);
  const [error, setError] = useState("");
  const pollRef = useRef(null);

  const refresh = useCallback(async () => {
    try {
      setState(await fetchState(context));
    } catch {
      /* backend unreachable: keep the last truthful state */
    }
  }, [context]);

  useEffect(() => {
    void refresh();
    const timer = setInterval(refresh, 30000);
    return () => clearInterval(timer);
  }, [refresh]);

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
          const result = restartStatusForRequest(payload, requestId);
          if (result.status === "succeeded") {
            clearInterval(pollRef.current);
            window.location.reload();
          }
          if (result.status === "failed") {
            clearInterval(pollRef.current);
            setSwitching(false);
            setError(result.error || t("switchControl.buildFailed", "Frontend build failed — still on the previous line"));
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

  useEffect(() => {
    const request = activeSwitchRequest(state);
    if (!request) return;
    setSwitching(true);
    setOpen(false);
    waitForNewLine(request.request_id);
  }, [state, waitForNewLine]);

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
        const redirect = switchTargetUrl(payload) || redirectUrlForLine(state, target, window.location.href);
        if (payload.status === "succeeded" && redirect) {
          window.location.assign(redirect);
          return;
        }
        waitForNewLine(payload.request_id);
      } catch (e) {
        setSwitching(false);
        setError(e instanceof Error ? e.message : String(e));
        void refresh();
      }
    },
    [context, refresh, state, waitForNewLine],
  );

  if (!state || !state.switchable) return null;
  const active = state.active_line || "?";
  const pointerStatus = (state.pointer && state.pointer.status) || "";
  const isSwitching = switching || Boolean(activeSwitchRequest(state));
  const label = isSwitching
    ? t("switchControl.switching", "Switching…")
    : `${t("switchControl.line", "Line")}: ${active}`;

  const children = [
    React.createElement(
      "button",
      {
        key: "btn",
        type: "button",
        className: "setup-btn switch-control-btn",
        disabled: isSwitching,
        title: t("switchControl.tooltip", "Switch the running app between the main and dev lines"),
        "aria-label": label,
        "aria-busy": isSwitching,
        onClick: () => setOpen((value) => !value),
      },
      isSwitching ? React.createElement("span", { className: "switch-control-spinner" }, "⟳ ") : null,
      label,
    ),
  ];

  if (open && !isSwitching) {
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
