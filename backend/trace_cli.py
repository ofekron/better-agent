#!/usr/bin/env python3
"""CLI for inspecting orchestration traces.

Usage:
  python trace_cli.py list [--session ID] [--limit N] [--json]
  python trace_cli.py show TRACE_ID [--step N] [--prompt] [--output] [--json]
  python trace_cli.py latest [--session ID] [--json]
  python trace_cli.py grep PATTERN [--field prompts|outputs|all] [--step-type TYPE] [--json]
  python trace_cli.py stats [--session ID] [--json]
  python trace_cli.py watch [--session ID]
  python trace_cli.py steps TRACE_ID [--json]
  python trace_cli.py diff TRACE_ID_1 TRACE_ID_2

Designed for both human inspection and agent consumption (--json).
"""

import argparse
import json
import sys
import time
from pathlib import Path

# Import from same directory
sys.path.insert(0, str(Path(__file__).parent))
import trace_collector


# ── Formatting helpers ────────────────────────────────────────────

DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
CYAN = "\033[36m"

STEP_COLORS = {
    "routing": MAGENTA,
    "thread_execution": BLUE,
    "context_distill": YELLOW,
    "context_inject": CYAN,
    "summary": GREEN,
}


def _no_color():
    """Disable colors (for piping)."""
    global DIM, BOLD, RESET, RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN
    DIM = BOLD = RESET = RED = GREEN = YELLOW = BLUE = MAGENTA = CYAN = ""
    for k in list(STEP_COLORS):
        STEP_COLORS[k] = ""


def fmt_duration(ms):
    if ms is None:
        return "..."
    if ms < 1000:
        return f"{int(ms)}ms"
    return f"{ms / 1000:.1f}s"


def fmt_tokens(usage):
    if not usage:
        return "-"
    inp = usage.get("input_tokens", 0)
    out = usage.get("output_tokens", 0)
    total = inp + out
    if total == 0:
        return "-"
    return f"{total:,} ({inp:,}in/{out:,}out)"


def truncate(s, maxlen=80):
    s = s.replace("\n", " ").strip()
    if len(s) > maxlen:
        return s[:maxlen - 3] + "..."
    return s


# ── Commands ──────────────────────────────────────────────────────

def cmd_list(args):
    entries = trace_collector.list_traces(session_id=args.session, limit=args.limit)
    if args.json:
        print(json.dumps(entries, indent=2))
        return
    if not entries:
        print("No traces found.")
        return

    print(f"\n{BOLD}{'TRACE ID':<20} {'TIMESTAMP':<20} {'STEPS':>5} {'DURATION':>10} {'TOKENS':>14} {'PROMPT'}{RESET}")
    print("─" * 100)
    for e in entries:
        tid = e.get("trace_id", "?")[:18]
        ts = e.get("timestamp", "")[:19]
        steps = e.get("step_count", 0)
        dur = fmt_duration(e.get("duration_ms"))
        tok = fmt_tokens(e.get("total_token_usage"))
        prompt = truncate(e.get("user_prompt_preview", ""), 30)
        print(f"{DIM}{tid:<20}{RESET} {ts:<20} {steps:>5} {dur:>10} {tok:>14} {prompt}")
    print()


def cmd_show(args):
    trace = trace_collector.get_trace(args.trace_id)
    if not trace:
        print(f"Trace not found: {args.trace_id}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(trace, indent=2))
        return

    # Header
    print(f"\n{BOLD}Trace: {trace['trace_id']}{RESET}")
    print(f"Session: {trace['session_id']}")
    print(f"Timestamp: {trace['timestamp']}")
    print(f"Duration: {fmt_duration(trace.get('duration_ms'))}")
    print(f"Tokens: {fmt_tokens(trace.get('total_token_usage'))}")
    print(f"Prompt: {trace.get('user_prompt', '')[:200]}")
    print()

    steps = trace.get("steps", [])

    # If a specific step is requested
    if args.step is not None:
        if args.step >= len(steps):
            print(f"Step {args.step} does not exist (trace has {len(steps)} steps)", file=sys.stderr)
            sys.exit(1)
        _print_step_detail(steps[args.step], args.step, show_prompt=args.prompt, show_output=args.output)
        return

    # Show all steps as waterfall
    max_dur = max((s.get("duration_ms") or 0) for s in steps) if steps else 1
    print(f"{BOLD}{'#':>3} {'TYPE':<20} {'THREAD':<16} {'DURATION':>10} {'TOKENS':>12} {'BAR'}{RESET}")
    print("─" * 90)
    for i, step in enumerate(steps):
        color = STEP_COLORS.get(step.get("step_type", ""), "")
        stype = step.get("step_type", "?")
        eph = " (eph)" if step.get("ephemeral") else ""
        thread = truncate(step.get("thread_name") or "", 14)
        dur = fmt_duration(step.get("duration_ms"))
        tok = fmt_tokens(step.get("token_usage"))
        bar_len = int(40 * (step.get("duration_ms") or 0) / max_dur) if max_dur > 0 else 0
        bar = "█" * max(bar_len, 1)
        print(f"{i:>3} {color}{stype + eph:<20}{RESET} {thread:<16} {dur:>10} {tok:>12} {color}{bar}{RESET}")
    print()
    print(f"{DIM}Use --step N to see full detail. Add --prompt and/or --output for content.{RESET}")
    print()


def _print_step_detail(step, index, show_prompt=False, show_output=False):
    color = STEP_COLORS.get(step.get("step_type", ""), "")
    print(f"{BOLD}Step #{index}: {color}{step.get('step_type', '?')}{RESET}")
    if step.get("thread_name"):
        print(f"Thread: {step['thread_name']} ({step.get('thread_id', '?')})")
    if step.get("ephemeral"):
        print(f"{YELLOW}Ephemeral (no-session-persistence){RESET}")
    print(f"Duration: {fmt_duration(step.get('duration_ms'))}")
    print(f"Tokens: {fmt_tokens(step.get('token_usage'))}")
    if step.get("error"):
        print(f"{RED}Error: {step['error']}{RESET}")
    if step.get("parse_error"):
        print(f"{YELLOW}Parse error: {step['parse_error']}{RESET}")

    prompt = step.get("input_prompt", "")
    output = step.get("raw_output", "")
    print(f"Input prompt: {len(prompt):,} chars")
    print(f"Raw output: {len(output):,} chars")

    if step.get("parsed_output"):
        print(f"\n{BOLD}Parsed output:{RESET}")
        print(json.dumps(step["parsed_output"], indent=2))

    if show_prompt:
        print(f"\n{BOLD}── Input Prompt ──{RESET}")
        print(prompt)

    if show_output:
        print(f"\n{BOLD}── Raw Output ──{RESET}")
        print(output)

    if not show_prompt and not show_output:
        print(f"\n{DIM}Add --prompt to see input, --output to see output, or both.{RESET}")
    print()


def cmd_latest(args):
    trace = trace_collector.get_latest_trace(session_id=args.session)
    if not trace:
        if args.json:
            print(json.dumps(None))
        else:
            print("No traces found.")
        return

    if args.json:
        print(json.dumps(trace, indent=2))
        return

    # Reuse show logic
    args.trace_id = trace["trace_id"]
    args.step = None
    args.prompt = False
    args.output = False
    cmd_show(args)


def cmd_grep(args):
    matches = trace_collector.grep_traces(
        pattern=args.pattern,
        field=args.field,
        session_id=args.session,
        step_type=args.step_type,
        limit=args.limit,
    )

    if args.json:
        print(json.dumps(matches, indent=2))
        return

    if not matches:
        print("No matches found.")
        return

    print(f"\n{BOLD}Found {len(matches)} match(es) for '{args.pattern}':{RESET}\n")
    for m in matches:
        color = STEP_COLORS.get(m.get("step_type", ""), "")
        tid = m.get("trace_id", "?")[:18]
        field = m.get("matched_field", "?")
        stype = m.get("step_type", "?")
        thread = m.get("thread_name") or ""
        ctx = m.get("match_context", "").replace("\n", " ")
        print(f"  {DIM}{tid}{RESET}  step {m.get('step_index', '?')}  {color}{stype:<20}{RESET} {thread}")
        print(f"    {DIM}[{field}]{RESET} ...{ctx}...")
        print()


def cmd_stats(args):
    stats = trace_collector.get_trace_stats(session_id=args.session)

    if args.json:
        print(json.dumps(stats, indent=2))
        return

    print(f"\n{BOLD}Trace Statistics{RESET}")
    if args.session:
        print(f"Session: {args.session}")
    print(f"Total traces: {stats.get('count', 0)}")
    print(f"Total duration: {fmt_duration(stats.get('total_duration_ms'))}")
    print(f"Avg duration: {fmt_duration(stats.get('avg_duration_ms'))}")
    print(f"Total tokens: {fmt_tokens(stats.get('total_token_usage'))}")
    print(f"Total steps: {stats.get('total_steps', 0)}")

    step_counts = stats.get("step_type_counts", {})
    if step_counts:
        print(f"\n{BOLD}Step type breakdown (from recent traces):{RESET}")
        for st, count in sorted(step_counts.items(), key=lambda x: -x[1]):
            color = STEP_COLORS.get(st, "")
            print(f"  {color}{st:<22}{RESET} {count}")
    print()


def cmd_steps(args):
    trace = trace_collector.get_trace(args.trace_id)
    if not trace:
        print(f"Trace not found: {args.trace_id}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(trace.get("steps", []), indent=2))
        return

    steps = trace.get("steps", [])
    for i, step in enumerate(steps):
        color = STEP_COLORS.get(step.get("step_type", ""), "")
        eph = " [eph]" if step.get("ephemeral") else ""
        thread = f" → {step['thread_name']}" if step.get("thread_name") else ""
        dur = fmt_duration(step.get("duration_ms"))
        prompt_len = len(step.get("input_prompt", ""))
        output_len = len(step.get("raw_output", ""))
        print(f"  {BOLD}#{i}{RESET} {color}{step.get('step_type', '?')}{eph}{RESET}{thread}  {dur}  prompt:{prompt_len:,}ch  output:{output_len:,}ch")
    print()


def cmd_watch(args):
    """Tail the index file, printing new traces as they arrive."""
    index_path = trace_collector._traces_dir() / "index.jsonl"
    print(f"{BOLD}Watching for new traces...{RESET} (Ctrl+C to stop)\n")

    # Start from end of file
    if index_path.exists():
        pos = index_path.stat().st_size
    else:
        pos = 0

    try:
        while True:
            if not index_path.exists():
                time.sleep(1)
                continue

            size = index_path.stat().st_size
            if size > pos:
                with open(index_path, encoding="utf-8") as f:
                    f.seek(pos)
                    new_data = f.read()
                    pos = f.tell()

                for line in new_data.strip().splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if args.session and entry.get("session_id") != args.session:
                        continue

                    tid = entry.get("trace_id", "?")[:18]
                    ts = entry.get("timestamp", "")[:19]
                    steps = entry.get("step_count", 0)
                    dur = fmt_duration(entry.get("duration_ms"))
                    tok = fmt_tokens(entry.get("total_token_usage"))
                    prompt = truncate(entry.get("user_prompt_preview", ""), 40)
                    print(f"{GREEN}NEW{RESET} {DIM}{tid}{RESET}  {ts}  {steps} steps  {dur}  {tok}  {prompt}")

            time.sleep(0.5)
    except KeyboardInterrupt:
        print(f"\n{DIM}Stopped.{RESET}")


def cmd_diff(args):
    """Compare two traces side by side."""
    t1 = trace_collector.get_trace(args.trace_id_1)
    t2 = trace_collector.get_trace(args.trace_id_2)
    if not t1:
        print(f"Trace not found: {args.trace_id_1}", file=sys.stderr)
        sys.exit(1)
    if not t2:
        print(f"Trace not found: {args.trace_id_2}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps({"trace_1": t1, "trace_2": t2}, indent=2))
        return

    print(f"\n{BOLD}Trace Comparison{RESET}\n")
    print(f"{'':20} {BOLD}{'Trace 1':<30} {'Trace 2':<30}{RESET}")
    print("─" * 80)
    print(f"{'ID':<20} {t1['trace_id']:<30} {t2['trace_id']:<30}")
    print(f"{'Timestamp':<20} {t1.get('timestamp','')[:19]:<30} {t2.get('timestamp','')[:19]:<30}")
    print(f"{'Duration':<20} {fmt_duration(t1.get('duration_ms')):<30} {fmt_duration(t2.get('duration_ms')):<30}")
    print(f"{'Steps':<20} {t1.get('step_count',0):<30} {t2.get('step_count',0):<30}")
    print(f"{'Tokens':<20} {fmt_tokens(t1.get('total_token_usage')):<30} {fmt_tokens(t2.get('total_token_usage')):<30}")

    s1 = t1.get("steps", [])
    s2 = t2.get("steps", [])
    max_steps = max(len(s1), len(s2))

    print(f"\n{BOLD}Steps:{RESET}")
    for i in range(max_steps):
        left = s1[i] if i < len(s1) else None
        right = s2[i] if i < len(s2) else None
        l_type = left.get("step_type", "-") if left else "-"
        r_type = right.get("step_type", "-") if right else "-"
        l_dur = fmt_duration(left.get("duration_ms")) if left else "-"
        r_dur = fmt_duration(right.get("duration_ms")) if right else "-"
        l_color = STEP_COLORS.get(l_type, "")
        r_color = STEP_COLORS.get(r_type, "")
        diff_marker = " " if l_type == r_type else f"{RED}!{RESET}"
        print(f"  {diff_marker} #{i}  {l_color}{l_type:<20}{RESET} {l_dur:>8}   │   {r_color}{r_type:<20}{RESET} {r_dur:>8}")
    print()


# ── Main ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Inspect orchestration traces",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--no-color", action="store_true", help="Disable color output")
    sub = parser.add_subparsers(dest="command")

    # list
    p_list = sub.add_parser("list", aliases=["ls"], help="List recent traces")
    p_list.add_argument("--session", "-s", help="Filter by session ID")
    p_list.add_argument("--limit", "-n", type=int, default=20)
    p_list.add_argument("--json", "-j", action="store_true")

    # show
    p_show = sub.add_parser("show", aliases=["get"], help="Show a trace")
    p_show.add_argument("trace_id")
    p_show.add_argument("--step", type=int, default=None, help="Show specific step")
    p_show.add_argument("--prompt", "-p", action="store_true", help="Show full input prompt")
    p_show.add_argument("--output", "-o", action="store_true", help="Show full raw output")
    p_show.add_argument("--json", "-j", action="store_true")

    # latest
    p_latest = sub.add_parser("latest", help="Show the latest trace")
    p_latest.add_argument("--session", "-s", help="Filter by session ID")
    p_latest.add_argument("--json", "-j", action="store_true")

    # grep
    p_grep = sub.add_parser("grep", help="Deep search into trace prompts/outputs")
    p_grep.add_argument("pattern")
    p_grep.add_argument("--field", "-f", choices=["prompts", "outputs", "all"], default="all")
    p_grep.add_argument("--session", "-s", help="Filter by session ID")
    p_grep.add_argument("--step-type", "-t", help="Filter by step type")
    p_grep.add_argument("--limit", "-n", type=int, default=50)
    p_grep.add_argument("--json", "-j", action="store_true")

    # stats
    p_stats = sub.add_parser("stats", help="Show aggregate statistics")
    p_stats.add_argument("--session", "-s", help="Filter by session ID")
    p_stats.add_argument("--json", "-j", action="store_true")

    # steps
    p_steps = sub.add_parser("steps", help="List steps in a trace")
    p_steps.add_argument("trace_id")
    p_steps.add_argument("--json", "-j", action="store_true")

    # watch
    p_watch = sub.add_parser("watch", aliases=["tail"], help="Watch for new traces in real-time")
    p_watch.add_argument("--session", "-s", help="Filter by session ID")

    # diff
    p_diff = sub.add_parser("diff", help="Compare two traces")
    p_diff.add_argument("trace_id_1")
    p_diff.add_argument("trace_id_2")
    p_diff.add_argument("--json", "-j", action="store_true")

    args = parser.parse_args()

    if args.no_color or not sys.stdout.isatty():
        _no_color()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    cmd_map = {
        "list": cmd_list, "ls": cmd_list,
        "show": cmd_show, "get": cmd_show,
        "latest": cmd_latest,
        "grep": cmd_grep,
        "stats": cmd_stats,
        "steps": cmd_steps,
        "watch": cmd_watch, "tail": cmd_watch,
        "diff": cmd_diff,
    }
    cmd_map[args.command](args)


if __name__ == "__main__":
    main()
