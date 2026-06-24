const STYLE_ID = "ofek-dev-ask-view-styles";
const STYLE_TEXT = `
.ask-group{margin-bottom:24px}
.ask-group .message-group{margin-bottom:0}
.ask-group--resolved{opacity:.5;transition:opacity .2s ease}
.ask-group--resolved .ask-picker-row-actions,.ask-group--resolved .ask-picker-actions{display:none}
.ask-greeting{display:flex;align-items:flex-start;gap:14px;max-width:720px;width:100%;padding:18px 20px;background:linear-gradient(180deg,color-mix(in srgb,var(--accent,#7b68ee) 12%,var(--bubble-bg,#161b22)),var(--bubble-bg,#161b22));border:1px solid var(--border,#30363d);border-radius:14px;box-shadow:0 1px 2px rgba(0,0,0,.25)}
.ask-greeting-icon{flex:0 0 auto;width:42px;height:42px;display:grid;place-items:center;border-radius:11px;background:color-mix(in srgb,var(--accent,#7b68ee) 20%,transparent);color:var(--accent,#7b68ee);box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--accent,#7b68ee) 38%,transparent)}
.ask-greeting-copy{min-width:0;display:flex;flex-direction:column;gap:4px}
.ask-greeting-title{font-weight:650;font-size:1.12em;letter-spacing:.01em;color:var(--text-primary,#e6edf3)}
.ask-greeting-body{color:var(--text-secondary,#8b949e);line-height:1.5;font-size:.95em}
.ask-picker{align-self:flex-start;max-width:720px;width:100%;padding:16px 18px;background:var(--bubble-bg,#161b22);border:1px solid var(--border,#30363d);border-radius:14px;display:flex;flex-direction:column;gap:12px;box-shadow:0 1px 2px rgba(0,0,0,.2)}
.ask-picker-title{display:flex;align-items:center;gap:8px;font-weight:600;color:var(--text-primary,#e6edf3);font-size:1.02em}
.ask-picker-title-icon{display:inline-flex;color:var(--accent,#7b68ee)}
.ask-picker-list{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:8px}
.ask-picker-row{display:flex;align-items:center;gap:12px;flex-wrap:wrap;width:100%;padding:10px 12px;border:1px solid var(--border,#30363d);border-radius:10px;background:var(--row-bg,#21262d);color:var(--text-primary,#e6edf3);transition:border-color .15s ease,background .15s ease}
.ask-picker-row:hover{border-color:color-mix(in srgb,var(--accent,#7b68ee) 45%,var(--border,#30363d));background:var(--row-hover,#30363d)}
.ask-picker-row-chosen{border-color:var(--accent,#7b68ee);box-shadow:0 0 0 1px var(--accent,#7b68ee) inset;background:color-mix(in srgb,var(--accent,#7b68ee) 14%,var(--row-bg,#21262d))}
.ask-picker-row-info{display:flex;flex-direction:column;align-items:flex-start;text-align:start;min-width:0;flex:1 1 180px}
.ask-picker-row-actions{display:flex;gap:6px;flex:0 0 auto;margin-inline-start:auto}
.ask-picker-view-btn,.ask-picker-choose-btn{padding:6px 12px;border-radius:8px;cursor:pointer;font-weight:500;white-space:nowrap;transition:filter .15s ease,background .15s ease}
.ask-picker-view-btn{border:1px solid var(--border,#30363d);background:var(--bubble-bg,#161b22);color:var(--text-primary,#e6edf3)}
.ask-picker-view-btn:hover{background:var(--row-hover,#30363d)}
.ask-picker-choose-btn{border:1px solid var(--accent,#7b68ee);background:var(--accent,#7b68ee);color:#fff}
.ask-picker-choose-btn:hover{filter:brightness(1.08)}
.ask-picker-row-name{font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:100%}
.ask-picker-row-cwd{font-size:.85em;color:var(--text-muted,#6e7681);margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:100%;font-family:var(--font-mono,ui-monospace,Menlo,monospace)}
.ask-no-matches{display:flex;align-items:center;gap:8px;color:var(--text-muted,#6e7681);padding:4px 2px}
.ask-no-matches-icon{display:inline-flex;color:var(--text-muted,#6e7681);opacity:.8}
.ask-picker-error{color:var(--warning,#d29922);padding:8px 10px;border:1px solid color-mix(in srgb,var(--warning,#d29922) 40%,var(--border,#30363d));border-radius:8px;background:color-mix(in srgb,var(--warning,#d29922) 8%,var(--row-bg,#21262d));line-height:1.4}
.ask-picker-reasoning{font-size:.9em;color:var(--text-secondary,#8b949e);padding-top:8px;border-top:1px dashed var(--border,#30363d);line-height:1.45}
.ask-picker-resolution{font-size:.9em;font-weight:600;color:var(--text-muted,#6e7681);padding-top:4px}
.ask-create-anyway,.ask-never-mind{align-self:flex-start;padding:8px 14px;border-radius:8px;cursor:pointer;white-space:nowrap;font-weight:500;transition:filter .15s ease,background .15s ease}
.ask-create-anyway{border:1px solid var(--accent,#7b68ee);background:var(--accent,#7b68ee);color:#fff}
.ask-create-anyway:hover{filter:brightness(1.08)}
.ask-never-mind{border:1px solid var(--border,#30363d);background:var(--bubble-bg,#161b22);color:var(--text-primary,#e6edf3)}
.ask-never-mind:hover{background:var(--row-hover,#30363d)}
.ask-create-anyway:disabled,.ask-never-mind:disabled{opacity:.5;cursor:not-allowed}
.ask-picker-prompt-section{display:flex;flex-direction:column;gap:6px}
.ask-picker-prompt-toggle{align-self:flex-start;padding:4px 10px;border:1px solid var(--border,#30363d);background:var(--bubble-bg,#161b22);color:var(--text-muted,#6e7681);border-radius:6px;cursor:pointer;font-size:.85em}
.ask-picker-prompt-toggle:hover{background:var(--row-hover,#30363d)}
.ask-picker-prompt-full{margin:0;padding:10px 12px;background:var(--row-bg,#21262d);border:1px solid var(--border,#30363d);border-radius:8px;font-family:var(--font-mono,ui-monospace,Menlo,monospace);font-size:.85em;white-space:pre-wrap;word-break:break-word;max-height:320px;overflow-y:auto;color:var(--text-primary,#e6edf3)}
.ask-picker-actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
@media (max-width:600px){
  .ask-greeting{padding:14px 16px;gap:12px}
  .ask-greeting-icon{width:36px;height:36px;border-radius:10px}
  .ask-picker{padding:14px}
  .ask-picker-row{padding:10px}
  .ask-picker-row-actions{flex:1 1 100%;justify-content:flex-end}
}
`;

function ensureStyles() {
  if (typeof document === "undefined" || document.getElementById(STYLE_ID)) return;
  const style = document.createElement("style");
  style.id = STYLE_ID;
  style.textContent = STYLE_TEXT;
  document.head.appendChild(style);
}

// Lucide "sparkles" — matches the Ask quick-button icon.
function SparklesIcon({ React, size = 22 }) {
  const h = React.createElement;
  return h(
    "svg",
    { viewBox: "0 0 24 24", width: size, height: size, fill: "none", stroke: "currentColor", "stroke-width": 2, "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" },
    h("path", { d: "M9.937 15.5A2 2 0 0 0 8.5 14.063l-6.135-1.582a.5.5 0 0 1 0-.962L8.5 9.936A2 2 0 0 0 9.937 8.5l1.582-6.135a.5.5 0 0 1 .962 0L14.063 8.5A2 2 0 0 0 15.5 9.937l6.135 1.581a.5.5 0 0 1 0 .964L15.5 14.063a2 2 0 0 0-1.437 1.437l-1.582 6.135a.5.5 0 0 1-.962 0z" }),
    h("path", { d: "M20 3v4" }),
    h("path", { d: "M22 5h-4" }),
    h("path", { d: "M4 17v2" }),
    h("path", { d: "M5 18H3" }),
  );
}

// Lucide "search" — for the related-sessions picker header.
function SearchIcon({ React, size = 16 }) {
  const h = React.createElement;
  return h(
    "svg",
    { viewBox: "0 0 24 24", width: size, height: size, fill: "none", stroke: "currentColor", "stroke-width": 2, "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" },
    h("circle", { cx: 11, cy: 11, r: 8 }),
    h("path", { d: "m21 21-4.3-4.3" }),
  );
}

function AskGreeting({ React }) {
  const h = React.createElement;
  return h(
    "div",
    { className: "ask-greeting" },
    h("div", { className: "ask-greeting-icon" }, h(SparklesIcon, { React, size: 22 })),
    h(
      "div",
      { className: "ask-greeting-copy" },
      h("div", { className: "ask-greeting-title" }, "Ask"),
      h("div", { className: "ask-greeting-body" }, "Describe the task. If a related session already exists I'll surface it; otherwise I'll start a new one for you."),
    ),
  );
}

function SessionPicker({ React, context }) {
  const { useMemo, useState } = React;
  const h = React.createElement;
  const askResult = context.askResult || {};
  const [promptExpanded, setPromptExpanded] = useState(false);
  const matches = useMemo(() => {
    const byId = new Map();
    for (const session of Array.isArray(context.allSessions) ? context.allSessions : []) byId.set(session.id, session);
    return (askResult.session_ids || []).map((sid) => byId.get(sid)).filter(Boolean);
  }, [askResult, context.allSessions]);
  const hasPrompt = Boolean(askResult.prompt_preview);
  const createLabel = typeof context.createLabel === "string" ? context.createLabel : undefined;
  const onView = typeof context.onView === "function" ? context.onView : () => {};
  const onChoose = typeof context.onChoose === "function" ? context.onChoose : () => {};
  const onCreateNew = typeof context.onCreateNew === "function" ? context.onCreateNew : () => {};
  const onDismiss = typeof context.onDismiss === "function" ? context.onDismiss : undefined;
  const onApproveNew = typeof context.onApproveNew === "function" ? context.onApproveNew : undefined;
  const resolutionLabel = context.chosenSessionId === "__dismissed__"
    ? "Never minded"
    : context.chosenSessionId === "__new__"
      ? "New session selected"
      : undefined;
  if (askResult.create_new) {
    return h(
      "div",
      { className: "ask-picker" },
      h("div", { className: "ask-picker-title" }, h("span", { className: "ask-picker-title-icon" }, h(SparklesIcon, { React, size: 16 })), "Create new session for delegation?"),
      hasPrompt ? h("div", { className: "ask-picker-prompt-section" }, h("button", { type: "button", className: "ask-picker-prompt-toggle", onClick: () => setPromptExpanded((value) => !value) }, promptExpanded ? "Hide prompt" : "Show prompt"), promptExpanded ? h("pre", { className: "ask-picker-prompt-full" }, askResult.prompt_preview) : null) : null,
      h("div", { className: "ask-picker-actions" }, h("button", { type: "button", className: "ask-picker-choose-btn", onClick: onApproveNew }, "Create & run"), h("button", { type: "button", className: "ask-create-anyway", onClick: onCreateNew }, createLabel || "Cancel")),
    );
  }
  return h(
    "div",
    { className: "ask-picker" },
    h("div", { className: "ask-picker-title" }, h("span", { className: "ask-picker-title-icon" }, h(SearchIcon, { React, size: 16 })), "Related sessions"),
    matches.length === 0
      ? h("div", { className: askResult.error ? "ask-picker-error" : "ask-no-matches" }, askResult.error ? askResult.error : [h("span", { key: "ico", className: "ask-no-matches-icon" }, h(SearchIcon, { React, size: 15 })), "No related sessions found."])
      : h("ul", { className: "ask-picker-list" }, matches.map((session) => h("li", { key: session.id, className: `ask-picker-row ${session.id === context.chosenSessionId ? "ask-picker-row-chosen" : ""}` }, h("div", { className: "ask-picker-row-info", title: session.cwd || "" }, h("span", { className: "ask-picker-row-name" }, session.name || "(untitled)"), session.cwd ? h("span", { className: "ask-picker-row-cwd" }, session.cwd) : null), h("div", { className: "ask-picker-row-actions" }, h("button", { type: "button", className: "ask-picker-view-btn", onClick: () => onView(session) }, "View"), h("button", { type: "button", className: "ask-picker-choose-btn", onClick: () => onChoose(session) }, "Choose"))))),
    hasPrompt ? h("div", { className: "ask-picker-prompt-section" }, h("button", { type: "button", className: "ask-picker-prompt-toggle", onClick: () => setPromptExpanded((value) => !value) }, promptExpanded ? "Hide prompt" : "Show prompt"), promptExpanded ? h("pre", { className: "ask-picker-prompt-full" }, askResult.prompt_preview) : null) : null,
    askResult.reasoning ? h("div", { className: "ask-picker-reasoning" }, askResult.reasoning) : null,
    resolutionLabel ? h("div", { className: "ask-picker-resolution" }, resolutionLabel) : null,
    h("div", { className: "ask-picker-actions" },
      h("button", { type: "button", className: "ask-create-anyway", onClick: onCreateNew }, createLabel || "Create new session anyway"),
      onDismiss ? h("button", { type: "button", className: "ask-never-mind", onClick: onDismiss }, "Never Mind") : null,
    ),
  );
}

export function Component({ context, React }) {
  ensureStyles();
  if (context.slot === "ask-greeting") {
    return React.createElement(AskGreeting, { React });
  }
  return React.createElement(SessionPicker, { React, context });
}
