import { useEffect, useId, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import Editor from "@monaco-editor/react";
import { ProviderConfigSyncPage } from "@better-agent/provider-config-sync-ui";
import type { FileFocus } from "../types";
import { JsonNode } from "./JsonNode";
import { FileViewer } from "./FileViewer";
import { linkifyFilePaths } from "../utils/linkifyFilePaths";
import { openExternalLink } from "../utils/externalLink";
import { useConfigPanelContext } from "./configPanelContext";
import { useScaledMonacoFontSize } from "../utils/typography";
import { API } from "../api";
import Icon from "./Icon";

interface EditArgs {
  file_path: string;
  edits: {
    old_string: string;
    new_string: string;
  }[];
}

interface Props {
  tool: string;
  /** Usually a string (nns runner emits `args` as a pre-stringified blob),
   * but stale persisted sessions may have a raw object here — e.g. the
   * SDK-driven manager runner used to save `block.input` as a dict before
   * it learned to json-stringify. We normalize at the boundary so the
   * parsers and renderers below only ever see strings. */
  args: string | Record<string, unknown> | null | undefined;
  result?: string;
  onFileClick?: (path: string, focus?: FileFocus) => void;
  onViewDiff?: (path: string, oldStr: string, newStr: string) => void;
}

/** Coerce whatever shape `args` arrives in to a string the parsers below
 *  can safely run regex/JSON.parse on, and that React can safely render. */
function normalizeArgs(args: Props["args"]): string {
  if (args == null) return "";
  if (typeof args === "string") return args;
  try {
    return JSON.stringify(args);
  } catch {
    return String(args);
  }
}

const TOOL_ICONS: Record<string, string> = {
  Bash: "$",
  bash: "$",
  execute_command: "$",
  run_command: "$",
  Read: "R",
  read_file: "R",
  view_file: "R",
  Write: "W",
  write_file: "W",
  create_file: "W",
  Edit: "E",
  edit_file: "E",
  Glob: "?",
  glob: "?",
  list_directory: "?",
  list_dir: "?",
  Grep: "/",
  grep: "/",
  search: "/",
  grep_search: "/",
  Task: "T",
  Agent: "A",
  Skill: "S",
  TodoWrite: "L",
  apply_patch: "P",
};

function isAgentTool(tool: string): boolean {
  return ["Agent", "Task"].includes(tool);
}

function isCanvasTool(tool: string): boolean {
  return tool === "canvas" || tool === "mcp__canvas__canvas";
}

const CANVAS_EMBED_URL =
  typeof import.meta !== "undefined" && import.meta.env?.VITE_CANVAS_URL
    ? import.meta.env.VITE_CANVAS_URL
    : "http://localhost:5175";

interface AgentArgs {
  description?: string;
  subagent_type?: string;
  prompt?: string;
}

const FULL_HISTORY_FORK_AGENT_ERROR =
  "Full-history forked agents inherit the parent agent type, model, and reasoning effort";

/** Parse Agent/Task args from Python dict string or JSON */
function parseAgentArgs(args: string): AgentArgs | null {
  // Try JSON first
  try {
    const parsed = JSON.parse(args);
    if (parsed && typeof parsed === "object") return parsed;
  } catch (err) {
    void err;
  }
  // Try Python dict (single quotes → double quotes)
  try {
    const jsonified = args
      .replace(/'/g, '"')
      .replace(/\\"/g, '\\\\"'); // preserve escaped quotes
    const parsed = JSON.parse(jsonified);
    if (parsed && typeof parsed === "object") return parsed;
  } catch (err) {
    void err;
  }
  // Regex fallback for key fields
  const desc = args.match(/['"]description['"]\s*:\s*['"]([^'"]+)['"]/);
  const type = args.match(/['"]subagent_type['"]\s*:\s*['"]([^'"]+)['"]/);
  const prompt = args.match(/['"]prompt['"]\s*:\s*['"](.+)['"]\s*\}?$/);
  if (desc || type || prompt) {
    return {
      description: desc?.[1],
      subagent_type: type?.[1],
      prompt: prompt?.[1],
    };
  }
  return null;
}

interface BashArgs {
  command: string;
  description?: string;
}

/** Parse Bash args JSON (or Python-dict-like string) into command + description.
 *  agy's run_command tool uses `CommandLine` (and optional `Cwd`) instead of
 *  `command`; accept both shapes. */
function parseBashArgs(args: string): BashArgs | null {
  const pick = (parsed: unknown): BashArgs | null => {
    if (!parsed || typeof parsed !== "object") return null;
    const obj = parsed as Record<string, unknown>;
    const command = obj.command ?? obj.CommandLine;
    if (typeof command !== "string") return null;
    const description = obj.description ?? obj.Cwd;
    return { command, description: typeof description === "string" ? description : undefined };
  };
  try {
    const out = pick(JSON.parse(args));
    if (out) return out;
  } catch (err) {
    void err;
  }
  // Try Python-dict form
  try {
    const out = pick(JSON.parse(args.replace(/'/g, '"')));
    if (out) return out;
  } catch (err) {
    void err;
  }
  return null;
}

function isBashTool(tool: string): boolean {
  return ["Bash", "bash", "execute_command", "run_command"].includes(tool);
}

function isEditTool(tool: string): boolean {
  return ["Edit", "edit_file", "MultiEdit", "multi_edit"].includes(tool);
}

function isApplyPatchTool(tool: string): boolean {
  return tool === "apply_patch" || tool.endsWith(".apply_patch");
}

function isViewImageTool(tool: string): boolean {
  if (tool === "view_image") return true;
  const mcp = parseMcpName(tool);
  return mcp !== null && mcp.toolName === "view_image";
}

/** Parse an MCP tool name like "mcp__manager-delegate__delegate" into
 *  its server and tool parts for nicer header display. */
function parseMcpName(tool: string): { server: string; toolName: string } | null {
  if (!tool.startsWith("mcp__")) return null;
  const rest = tool.slice(5);
  const idx = rest.indexOf("__");
  if (idx === -1) return null;
  return { server: rest.slice(0, idx), toolName: rest.slice(idx + 2) };
}

/** Best-effort parse of args into a JSON object. Accepts real JSON or
 *  single-quoted Python-dict-like strings. Returns null if args isn't
 *  an object/array or doesn't parse. */
function tryParseJsonValue(args: string): unknown {
  const trimmed = args.trim();
  if (!trimmed) return null;
  if (!/^[{[]/.test(trimmed)) return null;
  try {
    return JSON.parse(trimmed);
  } catch (err) {
    void err;
  }
  try {
    return JSON.parse(trimmed.replace(/'/g, '"'));
  } catch (err) {
    void err;
  }
  return null;
}

function extractWrappedOutput(text: string): string | null {
  const outputMarker = "\nOutput:\n";
  const markerIndex = text.indexOf(outputMarker);
  if (markerIndex === -1) return null;
  return text.slice(markerIndex + outputMarker.length).trim();
}

function tryParseToolOutputJson(text: string): unknown {
  const wrappedOutput = extractWrappedOutput(text);
  if (!wrappedOutput) return null;
  return tryParseJsonValue(wrappedOutput);
}

/** Produce a compact one-line summary of parsed args for the header,
 *  leading with the most meaningful fields when they're present. */
function summarizeArgs(parsed: unknown): string {
  if (parsed === null || typeof parsed !== "object") return "";
  if (Array.isArray(parsed)) return `[${parsed.length}]`;
  const entries = Object.entries(parsed as Record<string, unknown>);
  if (entries.length === 0) return "{}";
  const preferred = [
    "instructions", "prompt", "command", "query", "pattern",
    "description", "file_path", "path", "url", "name", "title",
    // agy uses PascalCase input keys
    "CommandLine", "Query", "SearchPath", "AbsolutePath", "DirectoryPath",
  ];
  const sorted = [...entries].sort((a, b) => {
    const ai = preferred.indexOf(a[0]);
    const bi = preferred.indexOf(b[0]);
    return (ai === -1 ? 999 : ai) - (bi === -1 ? 999 : bi);
  });
  const render = (v: unknown): string => {
    if (v === null) return "null";
    if (typeof v === "string") {
      return `"${v.length > 40 ? v.slice(0, 40) + "\u2026" : v}"`;
    }
    if (typeof v === "object") {
      return Array.isArray(v) ? `[${(v as unknown[]).length}]` : "{\u2026}";
    }
    return String(v);
  };
  const parts = sorted.slice(0, 2).map(([k, v]) => `${k}: ${render(v)}`);
  const rest = entries.length > 2 ? `, +${entries.length - 2}` : "";
  return parts.join(", ") + rest;
}

/** Pull a file path out of parsed args when present — used so Read/Write/etc.
 *  render just the path in the header instead of the whole JSON blob. */
function extractFilePath(parsed: unknown): string | null {
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
  const obj = parsed as Record<string, unknown>;
  const candidates = ["file_path", "path", "notebook_path", "filePath", "AbsolutePath"];
  for (const k of candidates) {
    const v = obj[k];
    if (typeof v === "string" && v) return v;
  }
  return null;
}

function isFilePath(tool: string): boolean {
  return ["Read", "read_file", "view_file", "Write", "write_file", "create_file", "Edit", "edit_file"].includes(tool);
}

/** Drop provider-internal bookkeeping fields that are not real tool inputs.
 *  agy attaches `toolAction`/`toolSummary` to every tool payload; they are
 *  orchestration metadata, not arguments, so hide them from the rendered
 *  args rather than showing noise to the user. */
function stripInternalToolKeys(value: unknown): unknown {
  if (!value || typeof value !== "object" || Array.isArray(value)) return value;
  const INTERNAL = new Set(["toolAction", "toolSummary"]);
  const src = value as Record<string, unknown>;
  const hasInternal = Object.keys(src).some((k) => INTERNAL.has(k));
  if (!hasInternal) return value;
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(src)) {
    if (!INTERNAL.has(k)) out[k] = v;
  }
  return out;
}

function parseEditArgs(args: string): EditArgs | null {
  try {
    const parsed = JSON.parse(args);
    if (!parsed || typeof parsed.file_path !== "string") return null;
    if (typeof parsed.old_string === "string" && typeof parsed.new_string === "string") {
      return {
        file_path: parsed.file_path,
        edits: [{ old_string: parsed.old_string, new_string: parsed.new_string }],
      };
    }
    if (Array.isArray(parsed.edits)) {
      const edits = parsed.edits
        .filter((edit: unknown): edit is { old_string: string; new_string: string } => {
          if (!edit || typeof edit !== "object") return false;
          const record = edit as Record<string, unknown>;
          return typeof record.old_string === "string" && typeof record.new_string === "string";
        })
        .map((edit: { old_string: string; new_string: string }) => ({
          old_string: edit.old_string,
          new_string: edit.new_string,
        }));
      if (edits.length > 0) return { file_path: parsed.file_path, edits };
    }
  } catch (err) {
    void err;
  }
  return null;
}

interface PatchFile {
  path: string;
  action: "add" | "update" | "delete";
  added: number;
  removed: number;
  lines: { kind: "add" | "remove" | "context" | "meta"; text: string }[];
}

interface PatchSummary {
  files: PatchFile[];
  added: number;
  removed: number;
}

function patchTextFromArgs(args: string): string {
  const parsed = tryParseJsonValue(args);
  if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
    const value = (parsed as Record<string, unknown>).value;
    if (typeof value === "string") return value;
  }
  return args;
}

function parseApplyPatch(args: string): PatchSummary | null {
  const text = patchTextFromArgs(args).trim();
  if (!text.includes("*** Begin Patch") || !text.includes("*** End Patch")) return null;

  const files: PatchFile[] = [];
  let current: PatchFile | null = null;
  for (const line of text.split("\n")) {
    const fileMatch = line.match(/^\*\*\* (Add|Update|Delete) File: (.+)$/);
    if (fileMatch) {
      current = {
        path: fileMatch[2],
        action: fileMatch[1].toLowerCase() as PatchFile["action"],
        added: 0,
        removed: 0,
        lines: [],
      };
      files.push(current);
      continue;
    }

    if (!current) continue;
    if (line.startsWith("+") && !line.startsWith("+++")) {
      current.added += 1;
      current.lines.push({ kind: "add", text: line });
    } else if (line.startsWith("-") && !line.startsWith("---")) {
      current.removed += 1;
      current.lines.push({ kind: "remove", text: line });
    } else if (line.startsWith("@@") || line.startsWith("*** ")) {
      current.lines.push({ kind: "meta", text: line });
    } else {
      current.lines.push({ kind: "context", text: line });
    }
  }

  if (files.length === 0) return null;
  return {
    files,
    added: files.reduce((sum, file) => sum + file.added, 0),
    removed: files.reduce((sum, file) => sum + file.removed, 0),
  };
}

function ApplyPatchToolCall({ args, result, onFileClick }: {
  args: string;
  result?: string;
  onFileClick?: (path: string, focus?: FileFocus) => void;
}) {
  const [open, setOpen] = useState(true);
  const patch = parseApplyPatch(args);
  if (!patch) {
    return (
      <div className="tool-call">
        <div className="tool-call-header">
          <span className="tool-icon">P</span>
          <span className="tool-name">apply_patch</span>
        </div>
        <pre className="tool-result-content">{patchTextFromArgs(args)}</pre>
        {result && <ToolResult result={result} tool="apply_patch" onFileClick={onFileClick} />}
      </div>
    );
  }

  const primaryPath = patch.files[0]?.path ?? "";
  const fileLabel = patch.files.length === 1 ? primaryPath : `${patch.files.length} files`;
  return (
    <div className="tool-call apply-patch-tool-call">
      <div className="tool-call-header">
        <span className="tool-icon apply-patch-icon">P</span>
        <span className="tool-name">apply_patch</span>
        <span className="patch-stat patch-stat-add">+{patch.added}</span>
        <span className="patch-stat patch-stat-remove">-{patch.removed}</span>
        <span className="tool-args">{fileLabel}</span>
      </div>
      <div className="apply-patch-summary">
        <button
          className="tool-args-toggle apply-patch-summary-toggle"
          onClick={(e) => { e.stopPropagation(); setOpen((v) => !v); }}
        >
          <span className="diff-arrow">{open ? <Icon name="chevron-down" size={14} style={{ verticalAlign: "-2px" }} /> : <Icon name="chevron-right" size={14} style={{ verticalAlign: "-2px" }} />}</span>
          <span className="apply-patch-summary-paths">
            {patch.files.map((file) => file.path).join(", ")}
          </span>
        </button>
        {open && (
          <div className="apply-patch-files">
            {patch.files.map((file) => (
              <div className="apply-patch-file" key={`${file.action}:${file.path}`}>
                <div className="apply-patch-file-header">
                  <button
                    className="apply-patch-path"
                    onClick={(e) => {
                      e.stopPropagation();
                      onFileClick?.(file.path);
                    }}
                  >
                    {file.action} {file.path}
                  </button>
                  <span className="patch-stat patch-stat-add">+{file.added}</span>
                  <span className="patch-stat patch-stat-remove">-{file.removed}</span>
                </div>
                <pre className="apply-patch-diff">
                  {file.lines.map((line, index) => (
                    <div key={index} className={`apply-patch-line apply-patch-line-${line.kind}`}>
                      {line.text || " "}
                    </div>
                  ))}
                </pre>
              </div>
            ))}
          </div>
        )}
      </div>
      {result && <ToolResult result={result} tool="apply_patch" onFileClick={onFileClick} />}
    </div>
  );
}

function InlineDiff({ oldStr, newStr }: { oldStr: string; newStr: string }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(true);
  const oldLines = oldStr ? oldStr.split("\n") : [];
  const newLines = newStr ? newStr.split("\n") : [];
  const summary = `${oldLines.length} → ${newLines.length} lines`;

  return (
    <div className="edit-diff" onClick={(e) => e.stopPropagation()}>
      <button
        className="diff-toggle"
        onClick={() => setOpen((v) => !v)}
      >
        <span className="diff-arrow">{open ? <Icon name="chevron-down" size={14} style={{ verticalAlign: "-2px" }} /> : <Icon name="chevron-right" size={14} style={{ verticalAlign: "-2px" }} />}</span>
        {t('toolCall.diffLabel')} <span className="diff-summary">({summary})</span>
      </button>
      {open && (
        <pre className="diff-content">
          {oldLines.map((l, i) => (
            <div key={`r${i}`} className="diff-removed">- {l}</div>
          ))}
          {newLines.map((l, i) => (
            <div key={`a${i}`} className="diff-added">+ {l}</div>
          ))}
        </pre>
      )}
    </div>
  );
}

/** Detect if result text is file content with line numbers */
function isFileContent(text: string): boolean {
  return /^\s*\d+[→\t]/.test(text);
}

/**
 * Extract the line range covered by a `cat -n`-style file-content result.
 * The format Read produces is `{lineNumber}\t{content}` (or with `→`), so we
 * pull the first and last numeric prefixes we see. Returns null if the text
 * isn't line-numbered file content.
 */
function extractLineRange(text: string): FileFocus | null {
  if (!isFileContent(text)) return null;
  const lines = text.split("\n");
  const re = /^\s*(\d+)[→\t]/;
  let startLine: number | null = null;
  let endLine: number | null = null;
  for (const line of lines) {
    const m = re.exec(line);
    if (!m) continue;
    const n = parseInt(m[1], 10);
    if (startLine === null) startLine = n;
    endLine = n;
  }
  if (startLine === null || endLine === null) return null;
  return { startLine, endLine };
}

/**
 * Parse `cat -n`-style Read output into raw content + the line number the
 * first content line corresponds to. Lines without a numeric prefix
 * (trailing notes like "File has more lines.") are preserved as-is.
 */
function parseFileContent(text: string): { startLine: number; content: string } | null {
  if (!isFileContent(text)) return null;
  const re = /^\s*(\d+)[→\t]/;
  const lines = text.split("\n");
  let startLine: number | null = null;
  const out: string[] = [];
  for (const line of lines) {
    const m = re.exec(line);
    if (m) {
      if (startLine === null) startLine = parseInt(m[1], 10);
      out.push(line.slice(m[0].length));
    } else {
      out.push(line);
    }
  }
  if (startLine === null) return null;
  return { startLine, content: out.join("\n") };
}

const EXT_LANGUAGE: Record<string, string> = {
  ts: "typescript", tsx: "typescript", js: "javascript", jsx: "javascript",
  mjs: "javascript", cjs: "javascript",
  py: "python", rb: "ruby", go: "go", rs: "rust", java: "java",
  c: "c", h: "c", cpp: "cpp", cc: "cpp", cxx: "cpp", hpp: "cpp",
  cs: "csharp", swift: "swift", kt: "kotlin", scala: "scala",
  json: "json", yaml: "yaml", yml: "yaml", toml: "ini",
  md: "markdown", markdown: "markdown",
  html: "html", htm: "html", css: "css", scss: "scss", less: "less",
  sh: "shell", bash: "shell", zsh: "shell",
  sql: "sql", xml: "xml",
  ini: "ini", env: "ini", conf: "ini",
  dockerfile: "dockerfile",
};

function languageFromPath(path: string | undefined): string {
  if (!path) return "plaintext";
  const name = path.split("/").pop() ?? path;
  if (name.toLowerCase() === "dockerfile") return "dockerfile";
  const ext = name.split(".").pop()?.toLowerCase() ?? "";
  return EXT_LANGUAGE[ext] ?? "plaintext";
}

function FileContentMonaco({ text, filePath }: { text: string; filePath?: string }) {
  const parsed = useMemo(() => parseFileContent(text), [text]);
  const language = useMemo(() => languageFromPath(filePath), [filePath]);
  const monacoFontSize = useScaledMonacoFontSize(12);
  if (!parsed) {
    return <pre className="tool-result-content file-content">{text}</pre>;
  }
  const lineCount = parsed.content.split("\n").length;
  const height = Math.min(Math.max(lineCount * 20 + 12, 80), 400);
  const startLine = parsed.startLine;
  return (
    <div className="tool-result-monaco" style={{ height }}>
      <Editor
        height="100%"
        language={language}
        value={parsed.content}
        theme="vs-dark"
        options={{
          readOnly: true,
          minimap: { enabled: false },
          fontSize: monacoFontSize,
          lineNumbers: (n: number) => String(startLine + n - 1),
          scrollBeyondLastLine: false,
          wordWrap: "on",
          automaticLayout: true,
          renderLineHighlight: "none",
          folding: false,
        }}
      />
    </div>
  );
}

/** Detect webReader MCP tool result prefix */
const WEBREADER_PREFIX_RE = /^webReader_result_summary:\s*/;

interface WebReaderEntry {
  title?: string;
  description?: string;
  url?: string;
  content?: string;
}

/** Parse webReader result text into structured entries */
function parseWebReaderResult(text: string): WebReaderEntry[] | null {
  const match = WEBREADER_PREFIX_RE.exec(text);
  if (!match) return null;
  const jsonText = text.slice(match[0].length).trim();
  if (!jsonText.startsWith("[")) return null;
  try {
    const arr = JSON.parse(jsonText);
    if (!Array.isArray(arr)) return null;
    return arr.map((item: unknown) => {
      if (!item || typeof item !== "object") return {};
      const obj = item as Record<string, unknown>;
      // Result shape: { text: { title, description, url, content } }
      // or { text: "stringified-json", ... }
      const textVal = obj.text;
      if (textVal && typeof textVal === "object") {
        const t = textVal as Record<string, unknown>;
        return {
          title: typeof t.title === "string" ? t.title : undefined,
          description: typeof t.description === "string" ? t.description : undefined,
          url: typeof t.url === "string" ? t.url : undefined,
          content: typeof t.content === "string" ? t.content : undefined,
        };
      }
      if (typeof textVal === "string") {
        try {
          const parsed = JSON.parse(textVal);
          if (parsed && typeof parsed === "object") {
            const p = parsed as Record<string, unknown>;
            return {
              title: typeof p.title === "string" ? p.title : undefined,
              description: typeof p.description === "string" ? p.description : undefined,
              url: typeof p.url === "string" ? p.url : undefined,
              content: typeof p.content === "string" ? p.content : undefined,
            };
          }
        } catch { /* not JSON, fall through */ }
        return { content: textVal };
      }
      return {};
    });
  } catch {
    return null;
  }
}

function isWebReaderTool(tool: string): boolean {
  if (tool === "webReader") return true;
  const mcp = parseMcpName(tool);
  return mcp !== null && mcp.toolName === "webReader";
}

function WebReaderResult({ entries }: { entries: WebReaderEntry[] }) {
  return (
    <div className="webreader-results">
      {entries.map((entry, i) => {
        const hasContent = !!entry.content && entry.content.length > 0;
        const contentPreview: string | null = hasContent
          ? entry.content!.length > 200
            ? entry.content!.slice(0, 200) + "\u2026"
            : entry.content!
          : null;
        return (
          <WebReaderCard
            key={i}
            index={i}
            title={entry.title}
            description={entry.description}
            url={entry.url}
            content={entry.content}
            hasContent={!!hasContent}
            contentPreview={contentPreview}
          />
        );
      })}
    </div>
  );
}

interface RequirementMatch {
  text?: string;
  kind?: string;
  source?: string;
  polarity?: string;
  strength?: string;
  cwd?: string;
  edited_files?: string[];
  source_text?: string;
}

interface RequirementsResult {
  success?: boolean;
  count?: number;
  matches?: RequirementMatch[];
  freshness?: {
    fresh?: boolean;
    unhandled_prompts?: number;
    unit_sync?: {
      error?: string;
    };
  };
}

function isGetRequirementsTool(tool: string): boolean {
  const mcp = parseMcpName(tool);
  const toolName = (mcp?.toolName ?? tool).replace(/-/g, "_");
  const server = mcp?.server.replace(/-/g, "_");
  return toolName === "get_requirements" || server === "get_requirements";
}

function parseRequirementsResult(value: unknown): RequirementsResult | null {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return null;
  const result = value as RequirementsResult;
  return Array.isArray(result.matches) ? result : null;
}

function RequirementsResultView({ result, onFileClick }: { result: RequirementsResult; onFileClick?: (path: string, focus?: FileFocus) => void }) {
  const matches = result.matches ?? [];
  const freshness = result.freshness;
  return (
    <div className="requirements-results">
      <div className="requirements-status-row">
        <span className={result.success === false ? "requirements-status-bad" : "requirements-status-good"}>
          {result.success === false ? "failed" : "success"}
        </span>
        <span>{result.count ?? matches.length} matches</span>
        {freshness && (
          <span>
            {freshness.fresh === false
              ? `stale${freshness.unhandled_prompts ? ` by ${freshness.unhandled_prompts}` : ""}`
              : "fresh"}
          </span>
        )}
        {freshness?.unit_sync?.error && <span>{freshness.unit_sync.error}</span>}
      </div>
      {matches.map((match, index) => (
        <div className="requirements-match-card" key={`${index}-${match.text ?? ""}`}>
          <div className="requirements-match-header">
            <span className="requirements-match-index">{index + 1}</span>
            <div className="requirements-match-tags">
              {match.kind && <span>{match.kind}</span>}
              {match.source && <span>{match.source}</span>}
              {match.polarity && <span>{match.polarity}</span>}
              {match.strength && <span>{match.strength}</span>}
            </div>
          </div>
          {match.text && (
            <div className="requirements-match-text">{linkifyFilePaths(match.text, onFileClick)}</div>
          )}
          {match.cwd && <div className="requirements-match-meta">{match.cwd}</div>}
          {Array.isArray(match.edited_files) && match.edited_files.length > 0 && (
            <div className="requirements-match-files">
              {match.edited_files.map((path) => (
                <button
                  className="requirements-file-link"
                  key={path}
                  onClick={(e) => {
                    e.stopPropagation();
                    onFileClick?.(path);
                  }}
                >
                  {path}
                </button>
              ))}
            </div>
          )}
          {match.source_text && match.source_text !== match.text && (
            <details className="requirements-source">
              <summary>source prompt</summary>
              <div>{linkifyFilePaths(match.source_text, onFileClick)}</div>
            </details>
          )}
        </div>
      ))}
    </div>
  );
}

function WebReaderCard({
  index,
  title,
  description,
  url,
  content,
  hasContent,
  contentPreview,
}: {
  index: number;
  title?: string;
  description?: string;
  url?: string;
  content?: string;
  hasContent: boolean;
  contentPreview: string | null;
}) {
  const [expanded, setExpanded] = useState(false);
  const domain = url ? new URL(url).hostname.replace(/^www\./, "") : null;

  return (
    <div className="webreader-card">
      <div className="webreader-card-header" onClick={() => hasContent && setExpanded((v) => !v)}>
        <span className="webreader-card-num">{index + 1}</span>
        <div className="webreader-card-meta">
          {title && <div className="webreader-card-title">{title}</div>}
          {description && <div className="webreader-card-desc">{description}</div>}
          {url && (
            <a
              className="webreader-card-url"
              href={url}
              rel="noopener noreferrer"
              onClick={(e) => {
                e.preventDefault();
                e.stopPropagation();
                openExternalLink(url);
              }}
            >
              {domain}
            </a>
          )}
        </div>
        {hasContent && (
          <span className="webreader-card-toggle">
            <span className="diff-arrow">{expanded ? <Icon name="chevron-down" size={14} style={{ verticalAlign: "-2px" }} /> : <Icon name="chevron-right" size={14} style={{ verticalAlign: "-2px" }} />}</span>
          </span>
        )}
      </div>
      {!expanded && contentPreview && (
        <div className="webreader-card-preview">{contentPreview}</div>
      )}
      {expanded && content && (
        <div className="webreader-card-content">
          <pre>{content}</pre>
        </div>
      )}
    </div>
  );
}

function ToolResult({ result, tool, filePath, onFileClick }: { result: string; tool: string; filePath?: string; onFileClick?: (path: string, focus?: FileFocus) => void }) {
  const { t } = useTranslation();
  const [showResult, setShowResult] = useState(false);
  const clean = result.replace(/^[\p{Emoji_Presentation}\p{Emoji}\uFE0F\u200D]+\s*/u, "").trim();
  if (!clean) return null;

  // Strip "Result: " prefix if present
  const stripped = clean.replace(/^Result:\s*/i, "");

  // WebReader tool: parse and render as cards
  if (isWebReaderTool(tool)) {
    const entries = parseWebReaderResult(stripped);
    if (entries && entries.length > 0) {
      return (
        <div className="tool-result-block">
          <button
            className="tool-result-toggle"
            onClick={(e) => { e.stopPropagation(); setShowResult(!showResult); }}
          >
            <span className="diff-arrow">{showResult ? <Icon name="chevron-down" size={14} style={{ verticalAlign: "-2px" }} /> : <Icon name="chevron-right" size={14} style={{ verticalAlign: "-2px" }} />}</span>
            <span className="webreader-tag">Web</span>
            <span className="tool-result-preview">{entries.length} result{entries.length !== 1 ? "s" : ""}</span>
          </button>
          {showResult && (
            <div className="tool-result-content webreader-result-wrap">
              <WebReaderResult entries={entries} />
            </div>
          )}
        </div>
      );
    }
  }

  const isShort = stripped.length < 120 && !stripped.includes("\n");
  const isFile = isFileContent(stripped) || ["Read", "read_file"].includes(tool);
  // If the result is a JSON object/array, render it with a tree viewer
  // instead of raw text. Skip file-content to preserve the line-numbered
  // formatting from Read.
  const parsedJson = !isFile ? tryParseJsonValue(stripped) ?? tryParseToolOutputJson(stripped) : null;
  const requirementsResult = isGetRequirementsTool(tool) ? parseRequirementsResult(parsedJson) : null;

  if (requirementsResult) {
    const matches = requirementsResult.matches ?? [];
    const freshness = requirementsResult.freshness;
    const freshnessLabel = freshness?.fresh === false
      ? `stale${freshness.unhandled_prompts ? ` by ${freshness.unhandled_prompts}` : ""}`
      : freshness ? "fresh" : "";
    return (
      <div className="tool-result-block">
        <button
          className="tool-result-toggle"
          onClick={(e) => { e.stopPropagation(); setShowResult(!showResult); }}
        >
          <span className="diff-arrow">{showResult ? "\u25BC" : "\u25B6"}</span>
          <span className="requirements-tag">Req</span>
          <span className="tool-result-preview">
            {requirementsResult.count ?? matches.length} matches{freshnessLabel ? ` · ${freshnessLabel}` : ""}
          </span>
        </button>
        {showResult && (
          <div className="tool-result-content requirements-result-wrap">
            <RequirementsResultView result={requirementsResult} onFileClick={onFileClick} />
          </div>
        )}
      </div>
    );
  }

  // Short results: show inline (but if they're JSON, still use the tree)
  if (isShort && !parsedJson) {
    return (
      <div className="tool-result-inline">
        <span className="tool-result-text">{linkifyFilePaths(stripped, onFileClick)}</span>
      </div>
    );
  }

  // Longer results: collapsible
  const preview = parsedJson
    ? summarizeArgs(parsedJson) || stripped.split("\n")[0].slice(0, 60)
    : stripped.split("\n")[0].slice(0, 60);
  const charCount = stripped.length >= 1000
    ? ` (${(stripped.length / 1000).toFixed(1)}k chars)`
    : "";

  return (
    <div className="tool-result-block">
      <button
        className="tool-result-toggle"
        onClick={(e) => { e.stopPropagation(); setShowResult(!showResult); }}
      >
        <span className="diff-arrow">{showResult ? "\u25BC" : "\u25B6"}</span>
        {parsedJson != null && <span className="json-embedded-tag">{t('toolCall.jsonTag')}</span>}
        <span className="tool-result-preview">
          {linkifyFilePaths(preview, onFileClick, { sessionLinks: "static" })}
          {charCount}
        </span>
      </button>
      {showResult && (
        parsedJson ? (
          <div className="tool-result-content tool-result-json">
            <JsonNode value={parsedJson} defaultOpen />
          </div>
        ) : isFile ? (
          <FileContentMonaco text={stripped} filePath={filePath} />
        ) : (
          <pre className="tool-result-content">{linkifyFilePaths(stripped, onFileClick)}</pre>
        )
      )}
    </div>
  );
}

function AgentToolCall({ tool, args, result, onFileClick }: { tool: string; args: string; result?: string; onFileClick?: (path: string, focus?: FileFocus) => void }) {
  const { t } = useTranslation();
  const [showPrompt, setShowPrompt] = useState(false);
  const agentArgs = parseAgentArgs(args);
  const icon = TOOL_ICONS[tool] || "A";
  const spawnFailed = result?.includes(FULL_HISTORY_FORK_AGENT_ERROR) ?? false;

  const description = agentArgs?.description || t('toolCall.subAgent');
  const agentType = agentArgs?.subagent_type;
  const prompt = agentArgs?.prompt;

  return (
    <div className={`tool-call agent-tool-call ${spawnFailed ? "agent-tool-call-failed" : ""}`}>
      <div className="tool-call-header">
        <span className="tool-icon agent-icon">{icon}</span>
        <span className="tool-name">{spawnFailed ? t('toolCall.agentSpawnFailed') : (agentType || tool)}</span>
        <span className="agent-description">{description}</span>
      </div>
      {prompt && (
        <div className="agent-prompt-section">
          <button
            className="agent-prompt-toggle"
            onClick={(e) => { e.stopPropagation(); setShowPrompt(!showPrompt); }}
          >
            <span className="diff-arrow">{showPrompt ? "\u25BC" : "\u25B6"}</span>
            {t('toolCall.promptLabel')}
          </button>
          {showPrompt && (
            <pre className="agent-prompt-content">{prompt}</pre>
          )}
        </div>
      )}
      {result && <ToolResult result={result} tool={tool} onFileClick={onFileClick} />}
    </div>
  );
}

/** Skill tool: skill name is clickable and opens the skill's SKILL.md. */
function SkillToolCall({ tool, args, result, onFileClick }: { tool: string; args: string; result?: string; onFileClick?: (path: string, focus?: FileFocus) => void }) {
  const { t } = useTranslation();
  const [showPrompt, setShowPrompt] = useState(false);
  const parsed = parseSkillArgs(args);

  const skillName = parsed?.skill ?? "";
  const skillMd = skillName ? `.claude/skills/${skillName}/SKILL.md` : "";
  const description = parsed?.args ?? t('toolCall.subAgent');

  return (
    <div className="tool-call skill-tool-call">
      <div className="tool-call-header">
        <span className="tool-icon skill-icon">S</span>
        {skillName ? (
          <button
            className="skill-name-btn"
            title={`Open ${skillMd}`}
            onClick={(e) => {
              e.stopPropagation();
              onFileClick?.(skillMd);
            }}
          >
            {skillName}
          </button>
        ) : (
          <span className="tool-name">{tool}</span>
        )}
        <span className="agent-description">{description}</span>
      </div>
      {parsed?.prompt && (
        <div className="agent-prompt-section">
          <button
            className="agent-prompt-toggle"
            onClick={(e) => { e.stopPropagation(); setShowPrompt(!showPrompt); }}
          >
            <span className="diff-arrow">{showPrompt ? <Icon name="chevron-down" size={12} /> : <Icon name="chevron-right" size={12} />}</span>
            {t('toolCall.promptLabel')}
          </button>
          {showPrompt && (
            <pre className="agent-prompt-content">{parsed.prompt}</pre>
          )}
        </div>
      )}
      {result && <ToolResult result={result} tool={tool} onFileClick={onFileClick} />}
    </div>
  );
}

/** Parse Skill tool args to extract the skill name and optional prompt/args. */
function parseSkillArgs(args: string): { skill?: string; prompt?: string; args?: string } | null {
  try {
    const parsed = JSON.parse(args);
    if (parsed && typeof parsed === "object") {
      return {
        skill: parsed.skill ?? parsed.name,
        prompt: parsed.prompt ?? parsed.args,
        args: parsed.description ?? parsed.args,
      };
    }
  } catch (err) {
    void err;
  }
  // Regex fallback
  const skill = args.match(/['"]skill['"]\s*:\s*['"]([^'"]+)['"]/);
  const prompt = args.match(/['"]args['"]\s*:\s*['"]([^'"]+)['"]/);
  return { skill: skill?.[1], args: prompt?.[1] };
}

const BASH_RESULT_COLLAPSED_LINES = 6;
const BASH_RESULT_COLLAPSED_HEIGHT = 130;
const CODEX_BASH_RESULT_HEADER =
  /^(Chunk ID: [^\n]*\nWall time: [^\n]*\nProcess exited with code [^\n]*\nOriginal token count: [^\n]*\n)Output:\n?/;

function formatBashResult(result?: string): string {
  const cleanResult = result
    ?.replace(/^[\p{Emoji_Presentation}\p{Emoji}️‍]+\s*/u, "")
    .trim()
    .replace(/^Result:\s*/i, "") || "";

  const codexHeader = cleanResult.match(CODEX_BASH_RESULT_HEADER);
  if (!codexHeader) return cleanResult;

  const metadata = codexHeader[1].trimEnd();
  const output = cleanResult.slice(codexHeader[0].length).trim();
  return output ? `${output}\n\n${metadata}` : metadata;
}

function BashToolCall({ tool, args, result, onFileClick }: {
  tool: string;
  args: string;
  result?: string;
  onFileClick?: (path: string, focus?: FileFocus) => void;
}) {
  const [showFull, setShowFull] = useState(false);
  const bashArgs = parseBashArgs(args);
  const command = bashArgs?.command || args;
  const description = bashArgs?.description;
  const cleanResult = formatBashResult(result);

  const resultLines = cleanResult ? cleanResult.split("\n").length : 0;
  const needsCollapse = resultLines > BASH_RESULT_COLLAPSED_LINES;

  return (
    <div className="tool-call bash-tool-call">
      <div className="tool-call-header">
        <span className="tool-icon">$</span>
        <span className="tool-name">{tool === "execute_command" ? "Bash" : tool}</span>
        {description && <span className="bash-description">{description}</span>}
      </div>
      <div className="bash-command-line">
        <span className="bash-command">{command}</span>
      </div>
      {cleanResult && (
        <div className="bash-result">
          <pre
            className={`bash-result-content ${!showFull && needsCollapse ? "bash-result-collapsed" : ""}`}
            style={!showFull && needsCollapse ? { maxHeight: BASH_RESULT_COLLAPSED_HEIGHT } : undefined}
          >
            {linkifyFilePaths(cleanResult, onFileClick)}
          </pre>
          {needsCollapse && !showFull && (
            <button
              className="bash-result-expand"
              onClick={(e) => { e.stopPropagation(); setShowFull(true); }}
            >
              <Icon name="chevron-down" size={12} style={{ verticalAlign: "-2px" }} /> {resultLines} lines — show all
            </button>
          )}
        </div>
      )}
    </div>
  );
}

function toLine(v: unknown): number | undefined {
  if (typeof v === "number" && Number.isFinite(v)) return v;
  if (typeof v === "string" && v.trim() !== "") {
    const n = Number(v);
    return Number.isFinite(n) ? n : undefined;
  }
  return undefined;
}

/** Detect an `open_file_panel` MCP tool call in INLINE mode. Returns
 *  the resolved path + ranges, or null for panel-mode / missing-mode
 *  / non-matching tools (missing `mode` on old persisted events is
 *  treated as non-inline so they render as a normal tool card). */
function inlineOpenFilePanel(
  tool: string,
  argsStr: string,
  result: string | undefined,
): { path: string; focus?: FileFocus; select?: FileFocus } | null {
  const mcp = parseMcpName(tool);
  if (!mcp || mcp.toolName !== "open_file_panel") return null;
  const parsed = tryParseJsonValue(argsStr);
  if (!parsed || typeof parsed !== "object") return null;
  const a = parsed as Record<string, unknown>;
  if (a.mode !== "inline") return null;

  // Prefer the backend-resolved absolute path from the tool result;
  // fall back to the raw arg path while the call is still in flight.
  let path = "";
  if (result) {
    try {
      const r = JSON.parse(result) as { panel?: { path?: string } };
      if (r?.panel?.path) path = r.panel.path;
    } catch {
      /* result not (yet) JSON — fall back below */
    }
  }
  if (!path && typeof a.path === "string") path = a.path;
  if (!path) return null;

  const s = toLine(a.start_line);
  const e = toLine(a.end_line);
  const ss = toLine(a.selected_start);
  const se = toLine(a.selected_end);
  return {
    path,
    focus: s !== undefined && e !== undefined ? { startLine: s, endLine: e } : undefined,
    select:
      ss !== undefined && se !== undefined
        ? { startLine: ss, endLine: se }
        : undefined,
  };
}

/** Editable, scrollable file view embedded directly in the message.
 *  Initial height is sized to the agent's requested line range
 *  (clamped); the user can then scroll/edit the whole file. */
function InlineFilePanel({
  path,
  focus,
  select,
}: {
  path: string;
  focus?: FileFocus;
  select?: FileFocus;
}) {
  const [collapsed, setCollapsed] = useState(false);
  if (collapsed) {
    return (
      <div className="tool-call inline-file-collapsed">
        <span className="tool-icon">#</span>
        <button className="btn-small" onClick={() => setCollapsed(false)}>
          {path.split("/").pop() || path}
        </button>
      </div>
    );
  }
  const span = focus ? focus.endLine - focus.startLine + 1 : 12;
  const rows = Math.max(8, Math.min(24, span));
  const height = rows * 19 + 84; // editor rows + header/chrome
  return (
    <div className="inline-file-panel" style={{ height }}>
      <FileViewer
        filePath={path}
        focus={focus}
        select={select ?? null}
        onClose={() => setCollapsed(true)}
      />
    </div>
  );
}

function inlineViewImage(
  tool: string,
  argsStr: string,
  result: string | undefined,
): { path: string; src: string } | null {
  if (!isViewImageTool(tool)) return null;
  const parsed = tryParseJsonValue(argsStr);
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
  const args = parsed as Record<string, unknown>;
  let path = typeof args.path === "string" ? args.path.trim() : "";

  if (result) {
    const clean = result
      .replace(/^[\p{Emoji_Presentation}\p{Emoji}\uFE0F\u200D]+\s*/u, "")
      .trim()
      .replace(/^Result:\s*/i, "");
    const parsedResult = tryParseJsonValue(clean);
    if (parsedResult && typeof parsedResult === "object" && !Array.isArray(parsedResult)) {
      const panel = (parsedResult as Record<string, unknown>).panel;
      if (panel && typeof panel === "object" && !Array.isArray(panel)) {
        const resolved = (panel as Record<string, unknown>).path;
        if (typeof resolved === "string" && resolved.trim()) path = resolved.trim();
      }
    }
  }

  if (!path) return null;
  return {
    path,
    src: `${API}/api/file/raw?path=${encodeURIComponent(path)}&node_id=primary`,
  };
}

function InlineImageViewer({ path, src }: { path: string; src: string }) {
  const [collapsed, setCollapsed] = useState(false);
  const [failed, setFailed] = useState(false);
  const fileName = path.split("/").pop() || path;

  if (collapsed) {
    return (
      <div className="tool-call inline-image-collapsed">
        <span className="tool-icon">I</span>
        <button className="btn-small" onClick={() => setCollapsed(false)}>
          {fileName}
        </button>
      </div>
    );
  }

  return (
    <div className="tool-call inline-image-viewer">
      <div className="tool-call-header">
        <span className="tool-icon">I</span>
        <span className="tool-name">view_image</span>
        <span className="tool-args" title={path}>{path}</span>
        <button
          type="button"
          className="btn-small"
          onClick={(e) => {
            e.stopPropagation();
            setCollapsed(true);
          }}
        >
          Collapse
        </button>
      </div>
      {failed ? (
        <div className="inline-image-error">Image failed to load</div>
      ) : (
        <div className="inline-image-frame">
          <img src={src} alt={fileName} onError={() => setFailed(true)} />
        </div>
      )}
    </div>
  );
}

/** Detect an `open_config_panel` MCP tool call. Returns the resolved
 *  capability target from the backend tool-result (falling back to the
 *  raw args while the call is in flight), or null for non-matching tools. */
function inlineOpenConfigPanel(
  tool: string,
  argsStr: string,
  result: string | undefined,
): { capability_id: string; scope: "global" | "project"; cwd: string } | null {
  const mcp = parseMcpName(tool);
  if (!mcp || mcp.toolName !== "open_config_panel") return null;
  const parsed = tryParseJsonValue(argsStr);
  if (!parsed || typeof parsed !== "object") return null;
  const a = parsed as Record<string, unknown>;
  const capabilityId = typeof a.capability_id === "string" ? a.capability_id : "";
  if (!capabilityId) return null;

  // Prefer the backend-resolved target from the tool result; fall back
  // to the raw arg values while the call is still in flight.
  let scope: "global" | "project" =
    a.scope === "global" ? "global" : "project";
  let cwd = typeof a.cwd === "string" ? a.cwd : "";
  if (result) {
    try {
      const r = JSON.parse(result) as {
        panel?: { scope?: string; cwd?: string };
      };
      if (r?.panel?.scope === "global" || r?.panel?.scope === "project") {
        scope = r.panel.scope;
      }
      if (typeof r?.panel?.cwd === "string") cwd = r.panel.cwd;
    } catch {
      /* result not (yet) JSON — fall back to args */
    }
  }
  return { capability_id: capabilityId, scope, cwd };
}

/** Embedded provider-config-sync capability editor (same UI/UX as the
 *  configs page) plus a header button to pop it into the right side panel. */
function InlineConfigPanel({
  capabilityId,
  scope,
  cwd,
}: {
  capabilityId: string;
  scope: "global" | "project";
  cwd: string;
}) {
  const ctx = useConfigPanelContext();
  const myId = useId();
  const [collapsed, setCollapsed] = useState(false);
  const [sent, setSent] = useState(false);

  useEffect(() => {
    if (!ctx) return;
    ctx.claimInline(myId);
    return () => ctx.releaseInline(myId);
  }, [myId, ctx]);

  // Superseded by a newer inline config panel → terminal closed marker.
  const isActive = ctx ? ctx.activeInlineId === myId : true;
  if (!isActive) {
    return (
      <div className="tool-call inline-config-closed">
        <span className="tool-icon"><Icon name="settings" size={14} /></span>
        <span className="inline-config-closed-label">
          {capabilityId} · editing closed
        </span>
      </div>
    );
  }
  if (collapsed) {
    return (
      <div className="tool-call inline-file-collapsed">
        <span className="tool-icon"><Icon name="settings" size={14} /></span>
        <button className="btn-small" onClick={() => setCollapsed(false)}>
          {capabilityId}
        </button>
      </div>
    );
  }
  const handleOpenInPanel = () => {
    if (!ctx) return;
    ctx.open({ capability_id: capabilityId, scope, cwd });
    setSent(true);
  };
  return (
    <div className="inline-config-panel">
      <div className="inline-config-panel-header">
        <span className="inline-config-panel-title">
          <Icon name="settings" size={13} style={{ verticalAlign: "-2px" }} /> {capabilityId}
          <span className="inline-config-panel-scope"> · {scope}</span>
        </span>
        <div className="inline-config-panel-actions">
          <button
            type="button"
            className="btn-small"
            onClick={handleOpenInPanel}
            disabled={!ctx}
            title="Open in right panel"
          >
            {sent ? "Opened ✓" : "Open in panel"}
          </button>
          <button
            type="button"
            className="btn-small"
            onClick={() => setCollapsed(true)}
            aria-label="Collapse"
          >
            Collapse
          </button>
        </div>
      </div>
      <div className="inline-config-panel-body">
        {ctx ? (
          <ProviderConfigSyncPage
            open
            embedded
            cwd={scope === "project" ? cwd : null}
            initialCapabilityId={capabilityId}
            client={ctx.client}
            subscribeExternalChanges={ctx.subscribeExternalChanges}
            onClose={() => setCollapsed(true)}
          />
        ) : (
          <div className="provider-config-sync-empty">
            Config panel unavailable.
          </div>
        )}
      </div>
    </div>
  );
}

/** Canvas tool: parse board_id from result JSON, render inline iframe. */
function CanvasToolCall({ args, result }: { args: string; result?: string }) {
  const { t } = useTranslation();
  const [collapsed, setCollapsed] = useState(false);

  // Parse board_id from the result JSON
  const boardId = useMemo(() => {
    if (!result) return null;
    const clean = result.replace(/^[\p{Emoji_Presentation}\p{Emoji}️‍]+\s*/u, "").trim();
    const parsed = tryParseJsonValue(clean);
    if (!parsed || typeof parsed !== "object") return null;
    return (parsed as Record<string, unknown>).id as string ?? null;
  }, [result]);

  const parsedArgs = tryParseJsonValue(args);
  const action = String((parsedArgs as Record<string, unknown>)?.action ?? "read");

  return (
    <div className="tool-call canvas-tool-call" style={{ border: "3px solid red" }}>
      <div className="tool-call-header">
        <span className="tool-icon"><Icon name="palette" size={14} /></span>
        <span className="tool-name">{t("toolCall.canvasTitle", "Canvas")}</span>
        <span className="tool-args">{action}</span>
        <span style={{ color: "red", fontWeight: "bold", marginInlineStart: 8 }}>boardId={String(boardId)}</span>
        {boardId && (
          <button
            className="tool-args-expand-btn"
            onClick={(e) => { e.stopPropagation(); setCollapsed((v) => !v); }}
            title={collapsed ? "Show canvas" : "Collapse canvas"}
          >
            <span className="diff-arrow">{collapsed ? <Icon name="chevron-right" size={12} /> : <Icon name="chevron-down" size={12} />}</span>
          </button>
        )}
      </div>
      {boardId && !collapsed && (
        <div className="canvas-inline-wrapper">
          <iframe
            src={`${CANVAS_EMBED_URL}?embed=true&board=${boardId}`}
            title="Canvas Board"
            className="canvas-inline-iframe"
          />
        </div>
      )}
    </div>
  );
}

export function ToolCall({ tool, args, result, onFileClick, onViewDiff }: Props) {
  const argsStr = normalizeArgs(args);

  // INVARIANT: every hook call (`useTranslation`, `useState`, `useRef`,
  // `useEffect`) MUST run on every render BEFORE any conditional `return`.
  // ToolCall is rendered for many different `tool` values, and the parent
  // (MessageBubble's `renderTreeLevel`) can reuse the same React-keyed
  // instance across renders during streaming. The moment a re-render
  // takes a different branch below, React would throw "Rendered fewer/
  // more hooks than expected" — and with no error boundary directly
  // around ToolCall, the entire chat tree would unmount, leaving the
  // page blank.
  const { t } = useTranslation();
  const [showArgs, setShowArgs] = useState(false);
  const [headerExpanded, setHeaderExpanded] = useState(false);
  const toolArgsRef = useRef<HTMLSpanElement>(null);
  const [argsTruncated, setArgsTruncated] = useState(false);

  // Derive `filePath` (and its precursors) ahead of every early return
  // so the truncation `useEffect` below has a stable dependency on every
  // render. Branches that early-return (InlineFilePanel / Agent / Bash
  // / Skill / Canvas) discard these values; the ref they never attach to
  // stays null, which the effect's `if (el)` guard tolerates.
  const editArgs = isEditTool(tool) ? parseEditArgs(argsStr) : null;
  const parsedArgs = editArgs ? null : stripInternalToolKeys(tryParseJsonValue(argsStr));
  const parsedFilePath = extractFilePath(parsedArgs);
  // Header text: for Edit use the path from editArgs, for other file tools
  // prefer the parsed file_path, otherwise fall back to a short summary of
  // the parsed args (MCP & generic tools) or the raw args string.
  const filePath = editArgs
    ? editArgs.file_path
    : parsedFilePath ?? (parsedArgs ? summarizeArgs(parsedArgs) : argsStr);

  // Detect if header args text is truncated by the ellipsis CSS.
  useEffect(() => {
    const el = toolArgsRef.current;
    if (el && !headerExpanded) {
      setArgsTruncated(el.scrollWidth > el.clientWidth);
    }
  }, [filePath, headerExpanded]);

  // `open_file_panel` MCP tool, inline mode → render the file
  // embedded in the message. Panel-mode / old missing-mode events
  // fall through to the normal tool-call card.
  const inline = inlineOpenFilePanel(tool, argsStr, result);
  if (inline) {
    return (
      <InlineFilePanel
        path={inline.path}
        focus={inline.focus}
        select={inline.select}
      />
    );
  }

  // `open_config_panel` MCP tool → embed the configs-page capability
  // editor in the message, with a button to pop it into the right panel.
  const inlineCfg = inlineOpenConfigPanel(tool, argsStr, result);
  if (inlineCfg) {
    return (
      <InlineConfigPanel
        capabilityId={inlineCfg.capability_id}
        scope={inlineCfg.scope}
        cwd={inlineCfg.cwd}
      />
    );
  }

  const inlineImage = inlineViewImage(tool, argsStr, result);
  if (inlineImage) {
    return <InlineImageViewer path={inlineImage.path} src={inlineImage.src} />;
  }

  // Agent/Task tool: special rendering
  if (isAgentTool(tool)) {
    return <AgentToolCall tool={tool} args={argsStr} result={result} onFileClick={onFileClick} />;
  }

  // Canvas tool: render board inline as iframe
  if (isCanvasTool(tool)) {
    return <CanvasToolCall args={argsStr} result={result} />;
  }

  // Skill tool: clickable skill name opens the skill directory
  if (tool === "Skill") {
    return <SkillToolCall tool={tool} args={argsStr} result={result} onFileClick={onFileClick} />;
  }

  // Bash tool: description header + inline result
  if (isBashTool(tool)) {
    return <BashToolCall tool={tool} args={argsStr} result={result} onFileClick={onFileClick} />;
  }

  if (isApplyPatchTool(tool)) {
    return <ApplyPatchToolCall args={argsStr} result={result} onFileClick={onFileClick} />;
  }

  const mcp = parseMcpName(tool);
  const displayName = mcp ? `${mcp.server}/${mcp.toolName}` : tool;
  const icon = TOOL_ICONS[tool] || (mcp ? "#" : "#");
  const clickable = isFilePath(tool) && onFileClick && (parsedFilePath || (!parsedArgs && filePath));
  const clickablePath = parsedFilePath ?? filePath;
  // Show an expand toggle whenever args parses to an object/array with content
  // and isn't already fully represented by an Edit diff or a single file path.
  const hasExpandableArgs = !!parsedArgs && !editArgs && (
    Array.isArray(parsedArgs)
      ? (parsedArgs as unknown[]).length > 0
      : Object.keys(parsedArgs as Record<string, unknown>).length > (parsedFilePath ? 1 : 0)
  );

  // For Read tool calls, parse the line range out of the result so the
  // file viewer can jump to and highlight exactly what was read.
  const focus: FileFocus | undefined = (() => {
    if (!result) return undefined;
    if (!["Read", "read_file"].includes(tool)) return undefined;
    const cleaned = result
      .replace(/^[\p{Emoji_Presentation}\p{Emoji}\uFE0F\u200D]+\s*/u, "")
      .trim()
      .replace(/^Result:\s*/i, "");
    return extractLineRange(cleaned) ?? undefined;
  })();

  return (
    <div
      className={`tool-call ${clickable ? "clickable" : ""}`}
      onClick={() => clickable && onFileClick?.(clickablePath, focus)}
    >
      <div className="tool-call-header">
        <span className="tool-icon">{icon}</span>
        <span className="tool-name" title={tool}>{displayName}</span>
        {filePath && (
          <span
            ref={toolArgsRef}
            className={`tool-args ${headerExpanded ? "tool-args-expanded" : ""}`}
            title={headerExpanded ? undefined : filePath}
          >
            {filePath}
          </span>
        )}
        {(argsTruncated || headerExpanded) && (
          <button
            className="tool-args-expand-btn"
            onClick={(e) => { e.stopPropagation(); setHeaderExpanded(v => !v); }}
            title={headerExpanded ? t('toolCall.collapseTitle') : t('toolCall.showFullTextTitle')}
          >
            <span className="diff-arrow">{headerExpanded ? <Icon name="chevron-down" size={12} /> : <Icon name="chevron-right" size={12} />}</span>
          </button>
        )}
        {focus && (
          <span className="tool-line-range" title={`Jump to lines ${focus.startLine}\u2013${focus.endLine}`}>
            L{focus.startLine}&ndash;L{focus.endLine}
          </span>
        )}
        {editArgs && editArgs.edits.length === 1 && onViewDiff && (
          <button
            className="diff-panel-btn"
            title={t('toolCall.panelButtonTitle')}
            onClick={(e) => {
              e.stopPropagation();
              const edit = editArgs.edits[0];
              onViewDiff(editArgs.file_path, edit.old_string, edit.new_string);
            }}
          >
            {t('toolCall.panelButton')}
          </button>
        )}
      </div>
      {editArgs && (
        <div className="edit-diff-list">
          {editArgs.edits.map((edit, index) => (
            <InlineDiff
              key={`${index}:${edit.old_string}:${edit.new_string}`}
              oldStr={edit.old_string}
              newStr={edit.new_string}
            />
          ))}
        </div>
      )}
      {hasExpandableArgs && (
        <div className="tool-args-section" onClick={(e) => e.stopPropagation()}>
          <button
            className="tool-args-toggle"
            onClick={(e) => { e.stopPropagation(); setShowArgs(v => !v); }}
          >
            <span className="diff-arrow">{showArgs ? "\u25BC" : "\u25B6"}</span>
            {t('toolCall.argsLabel')}
          </button>
          {showArgs && (
            <div className="tool-args-tree">
              <JsonNode value={parsedArgs} defaultOpen />
            </div>
          )}
        </div>
      )}
      {result && <ToolResult result={result} tool={tool} filePath={parsedFilePath ?? undefined} onFileClick={onFileClick} />}
    </div>
  );
}
