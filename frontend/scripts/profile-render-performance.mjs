import { mkdtemp, readFile, rm, stat } from "node:fs/promises";
import { spawn } from "node:child_process";
import { createServer } from "node:http";
import { createHash } from "node:crypto";
import { tmpdir } from "node:os";
import { join } from "node:path";

const chrome = process.env.CHROME_BIN || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const fixturePort = 20222 + Math.floor(Math.random() * 1000);
const origin = `http://127.0.0.1:${fixturePort}`;
const sessions = ["fixture-a", "fixture-b"];

const viewports = [{ name: "compact", width: 390, height: 844 }, { name: "wide", width: 1440, height: 900 }];
const repetitions = Number(process.env.BA_PERF_REPETITIONS || 7);
const port = 19222 + Math.floor(Math.random() * 1000);
const profile = await mkdtemp(join(tmpdir(), "ba-render-profile-"));
const dist = new URL("../dist/", import.meta.url);

function nativeEvents(seed, scale = 18) {
  const events = [];
  for (let i = 0; i < scale; i++) {
    const toolId = `${seed}-tool-${i}`;
    events.push({ type: "agent_message", data: { type: "assistant", message: { role: "assistant", content: [
      { type: "thinking", thinking: `analysis ${i} `.repeat(30) },
      { type: "tool_use", id: toolId, name: i % 3 === 0 ? "ExtensionAction" : "Read", input: { opaque: i, count: 30 } },
    ] } } });
    events.push({ type: "agent_message", data: { type: "user", message: { role: "user", content: [
      { type: "tool_result", tool_use_id: toolId, content: `result ${i} `.repeat(80) },
    ] } } });
    events.push({ type: "agent_message", data: { type: "assistant", message: { role: "assistant", content: [
      { type: "text", text: `### Phase ${i}\n\n- generated row ${i}\n- responsive fixture\n\n\`\`\`json\n{"index":${i}}\n\`\`\`` },
    ] } } });
  }
  return events;
}
function fixtureSession(id, scale) {
  const messages = [];
  for (let i = 0; i < 3; i++) {
    messages.push({ id: `${id}-u-${i}`, seq: i * 2, role: "user", content: `Generated prompt ${i}`, events: [], timestamp: new Date(1700000000000 + i).toISOString(), isStreaming: false });
    messages.push({ id: `${id}-a-${i}`, seq: i * 2 + 1, role: "assistant", content: `Generated answer ${i}`, events: nativeEvents(`${id}-${i}`, scale + i * 4), timestamp: new Date(1700000001000 + i).toISOString(), isStreaming: false, workers: [] });
  }
  return { id, name: `Fixture ${id}`, cwd: "/fixture", provider_id: "codex", model: "fixture", orchestration_mode: "team", messages, forks: [], root_events: [], pagination: { total_messages: 6, oldest_loaded_seq: 0, has_older: false }, created_at: new Date(1700000000000).toISOString(), updated_at: new Date(1700000002000).toISOString() };
}
const fixtures = { "fixture-a": fixtureSession("fixture-a", 18), "fixture-b": fixtureSession("fixture-b", 28) };
const perfPayloads = [];
const jsonResponse = (res, body) => { res.writeHead(200, { "content-type": "application/json", "cache-control": "no-store" }); res.end(JSON.stringify(body)); };
const server = createServer(async (req, res) => {
  const url = new URL(req.url || "/", origin);
  if (url.pathname === "/api/logs/frontend" && req.method === "POST") {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    try { const payload = JSON.parse(Buffer.concat(chunks).toString("utf8")); if (payload.source === "render-perf") perfPayloads.push(payload); } catch {}
    return jsonResponse(res, {});
  }
  if (url.pathname === "/api/auth/me") return jsonResponse(res, { username: "fixture" });
  if (url.pathname === "/api/auth/needs_setup") return jsonResponse(res, { needs_setup: false });
  if (url.pathname === "/api/sessions") return jsonResponse(res, { sessions: Object.values(fixtures).map(({ messages, ...session }) => session), total: 2 });
  const match = url.pathname.match(/^\/api\/sessions\/(fixture-[ab])$/);
  if (match) return jsonResponse(res, fixtures[match[1]]);
  if (url.pathname === "/api/providers") return jsonResponse(res, { providers: [{ id: "codex", name: "Codex", kind: "codex", default_model: "fixture", custom_models: [], reasoning_effort_options: [] }], default_provider_id: "codex" });
  if (url.pathname === "/api/projects") return jsonResponse(res, []);
  if (url.pathname.startsWith("/api/")) return jsonResponse(res, {});
  let path = url.pathname === "/" || url.pathname.startsWith("/s/") ? "index.html" : url.pathname.slice(1);
  try { const file = new URL(path, dist); await stat(file); res.writeHead(200, { "content-type": path.endsWith(".js") ? "text/javascript" : path.endsWith(".css") ? "text/css" : "text/html" }); res.end(await readFile(file)); }
  catch { res.writeHead(404); res.end(); }
});
server.on("upgrade", (req, socket) => {
  const key = req.headers["sec-websocket-key"];
  if (!key) return socket.destroy();
  const accept = createHash("sha1").update(`${key}258EAFA5-E914-47DA-95CA-C5AB0DC85B11`).digest("base64");
  socket.write(`HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ${accept}\r\n\r\n`);
  let tick = 0;
  const timer = setInterval(() => {
    if (socket.destroyed || !socket.writable) return;
    const id = tick % 2 ? "fixture-a" : "fixture-b";
    const message = { id: `${id}-a-2`, seq: 5, role: "assistant", content: `stream-${tick}`, events: nativeEvents(`${id}-stream`, 3 + tick % 4), timestamp: new Date(1700000003000 + tick).toISOString(), isStreaming: true, workers: [] };
    const payload = Buffer.from(JSON.stringify({ type: "messages_delta", data: { app_session_id: id, messages: [message] } }));
    const header = payload.length < 126 ? Buffer.from([0x81, payload.length]) : Buffer.from([0x81, 126, payload.length >> 8, payload.length & 255]);
    socket.write(Buffer.concat([header, payload]));
    tick++;
  }, 180);
  socket.on("close", () => clearInterval(timer));
  socket.on("error", () => clearInterval(timer));
});
await new Promise((resolve) => server.listen(fixturePort, "127.0.0.1", resolve));
const proc = spawn(chrome, ["--headless=new", `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`, "--no-first-run", "about:blank"], { stdio: "ignore" });

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function json(url, init) { const response = await fetch(url, init); if (!response.ok) throw new Error(`${response.status} ${url}`); return response.json(); }
for (let i = 0; i < 50; i++) { try { await json(`http://127.0.0.1:${port}/json/version`); break; } catch { await sleep(100); } }
const target = await json(`http://127.0.0.1:${port}/json/new?${encodeURIComponent(origin)}`, { method: "PUT" });
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });
let seq = 0;
const pending = new Map();
const events = [];
let tracingCompleteResolve = null;
ws.onmessage = ({ data }) => {
  const msg = JSON.parse(data);
  if (msg.id) { const item = pending.get(msg.id); if (item) { pending.delete(msg.id); item.resolve(msg.result); } return; }
  if (msg.method === "Tracing.dataCollected") events.push(...msg.params.value);
  if (msg.method === "Tracing.tracingComplete") tracingCompleteResolve?.();
};
const cdp = (method, params = {}) => new Promise((resolve, reject) => {
  const id = ++seq; pending.set(id, { resolve, reject }); ws.send(JSON.stringify({ id, method, params }));
});

const percentile = (values, p) => values.length ? values[Math.min(values.length - 1, Math.floor(values.length * p))] : 0;
const metricSummary = (payloads) => {
  const grouped = {};
  for (const payload of payloads) {
    const match = String(payload.message || "").match(/^([^ ]+) (\{.*\})$/);
    if (!match) continue;
    let data; try { data = JSON.parse(match[2]); } catch { continue; }
    const duration = Number(data.duration_ms ?? data.render_to_commit_ms ?? data.read_ms);
    if (!Number.isFinite(duration)) continue;
    (grouped[match[1]] ||= []).push(duration);
  }
  return Object.fromEntries(Object.entries(grouped).map(([stage, values]) => { values.sort((a, b) => a - b); return [stage, { count: values.length, median_ms: percentile(values, .5), p95_ms: percentile(values, .95), max_ms: values.at(-1) }]; }));
};
try {
  await cdp("Page.enable"); await cdp("Runtime.enable");
  const report = {};
  for (const viewport of viewports) {
    const metricStart = perfPayloads.length;
    await cdp("Emulation.setDeviceMetricsOverride", { width: viewport.width, height: viewport.height, deviceScaleFactor: 1, mobile: viewport.name === "compact" });
    const durations = [];
    events.length = 0;
    await cdp("Tracing.start", { categories: "devtools.timeline,blink.user_timing", transferMode: "ReportEvents" });
    for (let i = 0; i < repetitions; i++) {
      for (const sid of sessions) {
        const started = performance.now();
        await cdp("Page.navigate", { url: `${origin}/s/${encodeURIComponent(sid)}?ba_perf=1` });
        await sleep(900);
        durations.push(performance.now() - started);
      }
    }
    const tracingComplete = new Promise((resolve) => { tracingCompleteResolve = resolve; });
    await cdp("Tracing.end");
    await tracingComplete;
    tracingCompleteResolve = null;
    const tasks = events.filter((event) => event.name === "RunTask" && event.ph === "X").map((event) => event.dur / 1000).filter((ms) => ms >= 50).sort((a, b) => a - b);
    const userTimings = events.filter((event) => event.cat?.includes("user_timing")).map(({ name, ts, dur = 0 }) => ({ name, ts, duration_ms: dur / 1000 }));
    report[viewport.name] = { repetitions, navigations: durations.length, longtasks: tasks.length, median_ms: percentile(tasks, .5), p95_ms: percentile(tasks, .95), max_ms: tasks.at(-1) || 0, phases: metricSummary(perfPayloads.slice(metricStart)), user_timing_count: userTimings.length };
  }
  process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
} finally {
  ws.close();
  const exited = new Promise((resolve) => proc.once("exit", resolve));
  proc.kill("SIGTERM");
  await exited;
  await new Promise((resolve) => server.close(resolve));
  await rm(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
}
