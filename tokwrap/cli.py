"""tokwrap: see where your Claude Code tokens and money went.

Usage:
  tokwrap                     last 30 days, every project
  tokwrap --days 7            a different window (0 = all time)
  tokwrap --project myapp     only projects whose folder name contains this
  tokwrap --session latest    one session (an id, a file path, or "latest")
  tokwrap --card card.svg     also save a share card (SVG; PNG too if Chrome is installed)
  tokwrap --json              machine-readable output

Reads ~/.claude/projects locally. Nothing is uploaded.
"""

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

from . import __version__
from .analyze import find_sessions, parse, session_key, session_rates

BAR = 24
REPLIES = "Claude's replies in context"
OUTPUT = "Claude's output"
USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def c(code, s):
    return f"\033[{code}m{s}\033[0m" if USE_COLOR else s


def fmt_tokens(n):
    for unit, size in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if n >= size:
            return f"{n / size:.1f}{unit}"
    return f"{n:.0f}"


def fmt_usd(x):
    return f"${x:,.2f}" if x < 1000 else f"${x:,.0f}"


def load(args):
    if args.session:
        path = resolve_session(args.session)
        if not path:
            sys.exit(f"tokwrap: no session found for {args.session!r}")
        files = [(path, path.parent.name, False)]
        sub = path.parent / path.stem / "subagents"
        if sub.is_dir():
            files += [(f, path.parent.name, True) for f in sub.glob("*.jsonl")]
    else:
        files = list(find_sessions(days=args.days, project=args.project))
    sessions = []
    seen = set()
    files.sort(key=lambda x: _mtime(x[0]))  # count each call in the oldest file that has it
    for i, (f, proj, is_sub) in enumerate(files):
        if sys.stderr.isatty() and len(files) > 20:
            print(f"\rReading {i + 1}/{len(files)} sessions…", end="", file=sys.stderr)
        s = parse(f, proj, seen, since=time.time() - args.days * 86400 if args.days and not args.session else 0)
        s.subagent = is_sub
        if s.calls:
            sessions.append(s)
    if sys.stderr.isatty() and len(files) > 20:
        print("\r" + " " * 40 + "\r", end="", file=sys.stderr)
    return sessions


def _mtime(f):
    try:
        return f.stat().st_mtime
    except OSError:
        return 0


def resolve_session(arg):
    p = Path(arg).expanduser()
    if p.is_file():
        return p
    files = [f for f, _, sub in find_sessions(days=0) if not sub]
    if arg == "latest":
        return max(files, key=lambda f: f.stat().st_mtime, default=None)
    return next((f for f in files if f.stem.startswith(arg)), None)


def summarize(sessions):
    rates, recorded, estimated = session_rates(sessions)
    cats = defaultdict(lambda: {"weight": 0.0, "usd": 0.0, "sent": 0.0})
    notices = defaultdict(lambda: [0.0, 0.0])
    items = []
    reread_tokens = 0.0
    reread_files = 0
    for s in sessions:
        rate = rates.get(session_key(s), 0.0)
        for b in s.blocks:
            key = "Subagents (their own context)" if s.subagent and b.category != "Claude's output" else b.category
            usd = b.weight * rate
            cats[key]["weight"] += b.weight
            cats[key]["usd"] += usd
            cats[key]["sent"] += b.sent
            if b.category == "Claude Code notices":
                notices[b.label][0] += b.weight
                notices[b.label][1] += usd
            if b.weight and b.category not in (OUTPUT, REPLIES):
                items.append((b.weight, usd, b.tokens, b.sent, b.category, b.label, s.project))
        for path, reads in s.reads.items():
            if len(reads) > 1:
                reread_files += 1
                reread_tokens += sum(b.sent for b in reads[1:])
    total_w = sum(v["weight"] for v in cats.values()) or 1
    main = [s for s in sessions if not s.subagent]
    return {
        "sessions": len(main),
        "subagent_runs": len(sessions) - len(main),
        "calls": sum(s.calls for s in sessions),
        "tokens": sum(s.input_tokens + s.output_tokens for s in sessions),
        "input_tokens": sum(s.input_tokens for s in sessions),
        "output_tokens": sum(s.output_tokens for s in sessions),
        "cost": recorded + estimated,
        "cost_recorded": recorded,
        "cost_estimated": estimated,
        "has_cost": recorded > 0,
        "categories": sorted(((k, v["usd"] / (recorded + estimated) if recorded else v["weight"] / total_w,
                               v["usd"], v["sent"]) for k, v in cats.items()), key=lambda x: -x[1]),
        "notices": sorted(((k, (usd / (recorded + estimated)) if recorded else w / total_w, usd)
                           for k, (w, usd) in notices.items()), key=lambda x: -x[1]),
        "items": [(w / total_w, usd, tok, sent, cat, label, proj)
                  for w, usd, tok, sent, cat, label, proj in sorted(items, key=lambda x: -x[1])[:8]],
        "reread_files": reread_files,
        "reread_tokens": reread_tokens,
        "peak_context": max((s.peak_context for s in sessions), default=0),
        "avg_calls": sum(s.calls for s in main) / len(main) if main else 0,
        "resets": sum(s.resets for s in main),
        "startup_tokens": max((b.tokens for s in main for b in s.blocks if b.category == "Startup context"),
                              default=0),
    }


def fixes(r):
    """Suggestions, each tied to a number from the user's own data. Biggest first."""
    share = {k: (p, usd) for k, p, usd, _ in r["categories"]}
    out = []

    def money(k):
        p, usd = share.get(k, (0, 0))
        return (f"{fmt_usd(usd)}, " if r["has_cost"] else "") + f"{p:.0%} of the total"

    n = dict((k, (p, usd)) for k, p, usd in r["notices"])
    tool_lists = sum(n.get(k, (0, 0))[0] for k in ("Tool list updates", "MCP server instructions"))
    if tool_lists > 0.05:
        out.append((tool_lists, f"Tool lists and MCP instructions took {tool_lists:.0%}. Every time an MCP server "
                    "connects, reconnects or changes, Claude Code adds its tool list to the conversation again. "
                    "Turn off the MCP servers and plugins you don't use in this project (/mcp, /plugin)."))
    p = share.get("Startup context", (0, 0))[0]
    if r["startup_tokens"] > 15000 and p > 0.03:
        out.append((p, f"Your startup context is {fmt_tokens(r['startup_tokens'])} tokens ({money('Startup context')}). "
                    "It is sent with every call: system prompt, tool definitions, CLAUDE.md, skills and plugins. "
                    "Trim CLAUDE.md and remove plugins and skills you don't use."))
    p = share.get("Shell output", (0, 0))[0]
    if p > 0.05:
        out.append((p, f"Shell output took {money('Shell output')}. Long command output stays in the conversation "
                    "and is re-sent on every later call. Ask for `| tail -40` or `--quiet`, or use rtk to "
                    "shrink it."))
    p = share.get("File reads", (0, 0))[0]
    if p > 0.05 or r["reread_files"] > 5:
        out.append((p, f"File reads took {money('File reads')}. {r['reread_files']} files were read more than "
                    f"once; the repeat reads were re-sent {fmt_tokens(r['reread_tokens'])} tokens in total. Point Claude at the "
                    "lines that matter, or use a code index such as serena."))
    p = share.get(REPLIES, (0, 0))[0]
    if p > 0.1:
        out.append((p, f"Claude's own earlier replies and tool calls took {money(REPLIES)} as "
                    "they were re-sent. Long sessions carry everything forward: /clear between unrelated tasks "
                    "and /compact before the context gets long."))
    if r["peak_context"] > 200000:
        out.append((0.0, f"Your largest context reached {fmt_tokens(r['peak_context'])} tokens. Each call in that "
                    "session re-sent all of it. Splitting work into shorter sessions is the biggest single saving."))
    p = share.get(OUTPUT, (0, 0))[0]
    if p > 0.15:
        out.append((p, f"Claude's output took {money(OUTPUT)}. Output tokens cost "
                    "5 times input. Ask for shorter answers, or try caveman."))
    p = share.get("Subagent reports", (0, 0))[0]
    if p > 0.05:
        out.append((p, f"Subagent reports took {money('Subagent reports')}. Ask subagents for a short summary "
                    "instead of everything they found."))
    out.sort(key=lambda x: -x[0])
    return [t for _, t in out[:5]]


def print_report(r, title):
    w = sys.stdout.write
    w("\n" + c("1", f"tokwrap · {title}") + "\n")
    w(c("2", f"{r['sessions']} sessions, {r['subagent_runs']} subagent runs, {r['calls']:,} API calls") + "\n\n")
    if r["has_cost"]:
        w(f"  {c('1;33', fmt_usd(r['cost']))} at API prices    ")
    w(f"{c('1', fmt_tokens(r['input_tokens']))} input tokens sent    {c('1', fmt_tokens(r['output_tokens']))} output\n\n")

    w(c("1", "Where it went") + "\n")
    for name, p, usd, sent in r["categories"]:
        if p < 0.005:
            continue
        filled = round(p * BAR)
        bar = c("33", "█" * filled) + c("2", "░" * (BAR - filled))
        money = f"{fmt_usd(usd):>10}" if r["has_cost"] else ""
        w(f"  {bar} {p:4.0%} {money}  {name}\n")
    if r["notices"] and r["notices"][0][1] > 0.02:
        w(c("2", "\n  Claude Code notices, by kind:") + "\n")
        for name, p, usd in r["notices"][:4]:
            if p >= 0.005:
                w(c("2", f"    {p:4.0%}  {fmt_usd(usd) if r['has_cost'] else ''}  {name}") + "\n")

    w("\n" + c("1", "Biggest single items") + c("2", "  (size × number of calls that re-sent it)") + "\n")
    for p, usd, tok, sent, cat, label, proj in r["items"][:5]:
        money = f"{fmt_usd(usd):>8}" if r["has_cost"] else f"{p:5.1%}"
        w(f"  {money}  {fmt_tokens(tok):>6} tokens, sent {sent / tok if tok else 0:,.0f}×  {cat}: {label}\n")

    fx = fixes(r)
    if fx:
        w("\n" + c("1", "What to change") + "\n")
        for i, f in enumerate(fx, 1):
            w(f"  {i}. {f}\n")
    w("\n" + c("2", "Totals come from Claude Code's logs. The split inside each step is estimated from text length.")
      + "\n")
    if r["has_cost"]:
        est = (f", plus {fmt_usd(r['cost_estimated'])} estimated for sessions it didn't record"
               if r["cost_estimated"] >= 0.01 else "")
        w(c("2", f"Dollars are API prices, from the cost Claude Code recorded{est}. On a subscription, "
                 "read them as use of your limits.") + "\n")
    w("\n")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="tokwrap", description="See where your Claude Code tokens and money went.")
    ap.add_argument("--days", type=int, default=30, help="look back this many days (0 = all time, default 30)")
    ap.add_argument("--project", help="only projects whose folder name contains this text")
    ap.add_argument("--session", help='one session: an id, a .jsonl path, or "latest"')
    ap.add_argument("--card", metavar="FILE", help="save a share card (.svg; also .png if Chrome is installed)")
    ap.add_argument("--json", action="store_true", help="print JSON")
    ap.add_argument("--version", action="version", version=f"tokwrap {__version__}")
    args = ap.parse_args(argv)

    sessions = load(args)
    if not sessions:
        print("tokwrap: no Claude Code sessions found in ~/.claude/projects for that window.")
        return 1
    r = summarize(sessions)
    title = (f"session {args.session}" if args.session else
             (f"last {args.days} days" if args.days else "all time")) + (f" · {args.project}" if args.project else "")
    if args.json:
        r2 = dict(r, fixes=fixes(r), title=title)
        print(json.dumps(r2, indent=2, default=float))
    else:
        print_report(r, title)
    if args.card:
        from .card import write_card
        for p in write_card(r, title, args.card):
            print(f"Saved {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
