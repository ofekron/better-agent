import { mkdtemp, rm } from "node:fs/promises";
import { spawn } from "node:child_process";
import { tmpdir } from "node:os";
import { join } from "node:path";

const chrome = process.env.CHROME_BIN || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const origin = process.env.BA_PERF_ORIGIN || "http://127.0.0.1:4173";
const sessions = process.argv.slice(2);
if (sessions.length < 2) throw new Error("pass at least two opaque session ids for cold/warm navigation");

const viewports = [{ name: "compact", width: 390, height: 844 }, { name: "wide", width: 1440, height: 900 }];
const repetitions = Number(process.env.BA_PERF_REPETITIONS || 7);
const port = 19222 + Math.floor(Math.random() * 1000);
const profile = await mkdtemp(join(tmpdir(), "ba-render-profile-"));
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
ws.onmessage = ({ data }) => {
  const msg = JSON.parse(data);
  if (msg.id) { const item = pending.get(msg.id); if (item) { pending.delete(msg.id); item.resolve(msg.result); } return; }
  if (msg.method === "Tracing.dataCollected") events.push(...msg.params.value);
};
const cdp = (method, params = {}) => new Promise((resolve, reject) => {
  const id = ++seq; pending.set(id, { resolve, reject }); ws.send(JSON.stringify({ id, method, params }));
});

const percentile = (values, p) => values.length ? values[Math.min(values.length - 1, Math.floor(values.length * p))] : 0;
try {
  await cdp("Page.enable"); await cdp("Runtime.enable");
  const report = {};
  for (const viewport of viewports) {
    await cdp("Emulation.setDeviceMetricsOverride", { width: viewport.width, height: viewport.height, deviceScaleFactor: 1, mobile: viewport.name === "compact" });
    const durations = [];
    events.length = 0;
    await cdp("Tracing.start", { categories: "devtools.timeline,v8,blink.user_timing", transferMode: "ReportEvents" });
    for (let i = 0; i < repetitions; i++) {
      for (const sid of sessions) {
        const started = performance.now();
        await cdp("Page.navigate", { url: `${origin}/s/${encodeURIComponent(sid)}?ba_perf=1` });
        await sleep(2500);
        durations.push(performance.now() - started);
      }
    }
    await cdp("Tracing.end"); await sleep(1000);
    const tasks = events.filter((event) => event.name === "RunTask" && event.ph === "X").map((event) => event.dur / 1000).filter((ms) => ms >= 50).sort((a, b) => a - b);
    const userTimings = events.filter((event) => event.cat?.includes("user_timing")).map(({ name, ts, dur = 0 }) => ({ name, ts, duration_ms: dur / 1000 }));
    report[viewport.name] = { repetitions, navigations: durations.length, longtasks: tasks.length, median_ms: percentile(tasks, .5), p95_ms: percentile(tasks, .95), max_ms: tasks.at(-1) || 0, user_timings: userTimings };
  }
  process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
} finally {
  ws.close(); proc.kill("SIGTERM"); await rm(profile, { recursive: true, force: true });
}
