"""Read Claude Code session logs and work out where the tokens went.

Claude Code writes one JSONL file per session under ~/.claude/projects/. Each
API call is logged with its exact token usage. Between two calls, new things
enter the context: tool results, your messages, Claude Code's notices, and the
previous reply. Every later call sends them again (from the prompt cache, but
still billed). So a block's real cost is its size times the number of calls
that carried it.

How sizes are measured: the growth in context between two calls is exact (from
the usage numbers). That growth is split between the blocks added in that step
in proportion to their length in characters. Totals are exact; the split inside
one step is an estimate.

Cost: cache writes, cache reads, plain input and output are priced differently.
tokwrap weights them with Anthropic's standard ratios (cache read 0.1x input,
5-minute cache write 1.25x, 1-hour cache write 2x, output 5x), then scales the
weights to the cost Claude Code itself recorded for the session, when present.
"""

import calendar
import json
import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

READ, WRITE_5M, WRITE_1H, OUTPUT = 0.1, 1.25, 2.0, 5.0
RESET_DROP = 0.4  # context shrinking by more than this means /compact or /clear

SEARCH_TOOLS = {"Grep", "Glob", "LS", "ToolSearch"}
WEB_TOOLS = {"WebFetch", "WebSearch"}
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
NOTICE_NAMES = {
    "mcp_instructions_delta": "MCP server instructions",
    "deferred_tools_delta": "Tool list updates",
    "deferred_tools_record": "Tool list updates",
    "hook_success": "Hook output",
    "hook_additional_context": "Hook output",
    "edited_text_file": "File-changed notices",
    "skill_listing": "Skill list",
    "agent_listing_delta": "Agent list",
    "file": "Attached files",
}


@dataclass
class Block:
    category: str
    label: str
    chars: int
    tokens: float = 0.0       # measured size in context
    sent: float = 0.0         # tokens sent, summed over every call that carried it
    weight: float = 0.0       # price-weighted cost units


@dataclass
class Session:
    path: Path
    project: str
    started: float = 0.0
    calls: int = 0
    model: str = ""
    input_tokens: int = 0      # all input-side tokens: plain + cache writes + cache reads
    cache_read: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    weight: float = 0.0
    all_weight: float = 0.0    # price weight of every call in the file, counted or not
    recorded_cost: float = 0.0
    resets: int = 0
    peak_context: int = 0
    blocks: list = field(default_factory=list)
    reads: dict = field(default_factory=lambda: defaultdict(list))  # path -> [Block]
    subagent: bool = False


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for c in content:
            if isinstance(c, dict):
                if c.get("type") == "image":
                    out.append("x" * 6000)  # placeholder length; the usage delta corrects it
                else:
                    out.append(_text(c.get("text") or c.get("content") or ""))
            else:
                out.append(str(c))
        return "\n".join(out)
    if isinstance(content, dict):
        return json.dumps(content)
    return str(content or "")


def _strings(obj):
    """Every string value inside a JSON object, for notices stored as structured data."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if k != "type":
                yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


def _user_category(text):
    t = text.lstrip()
    if t.startswith("Base directory for this skill"):
        return "Skill instructions"
    if t.startswith(("<command-", "<local-command", "<task-notification", "Caveat:", "<system-reminder")):
        return "Claude Code notices"
    return "Your messages"


def _short(s, n=60):
    s = re.sub(r"\s+", " ", str(s)).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _tool_category(name, inp):
    if name == "Bash":
        return "Shell output", "Bash: " + _short(inp.get("command", ""), 50)
    if name == "Read":
        p = inp.get("file_path", "")
        return "File reads", "Read: " + _short(Path(p).name or p, 50)
    if name in SEARCH_TOOLS:
        return "Search results", f"{name}: " + _short(inp.get("pattern") or inp.get("query") or "", 45)
    if name in WEB_TOOLS:
        return "Web pages", f"{name}: " + _short(inp.get("url") or inp.get("query") or "", 45)
    if name in ("Agent", "Task"):
        return "Subagent reports", "Agent: " + _short(inp.get("description", ""), 45)
    if name in EDIT_TOOLS:
        return "Edits", f"{name}: " + _short(Path(inp.get("file_path", "")).name, 45)
    if name.startswith("mcp__"):
        server = name.split("__")[1] if name.count("__") >= 2 else name
        return f"MCP: {server}", _short(name, 60)
    return "Other tools", name


def parse(path, project=None, seen=None, since=0.0):
    """Parse one session file into a Session with priced blocks.

    `seen` is a set of API request ids shared across files. Resumed sessions copy
    their history into a new file; calls already counted elsewhere still shape the
    context here but add no tokens or cost.
    """
    seen = set() if seen is None else seen
    # Calls before `since` (a Unix time) shape the context but are not counted.
    s = Session(path=Path(path), project=project or Path(path).parent.name)
    tools = {}            # tool_use_id -> (name, input)
    pending = []          # blocks added since the last call
    live = []             # blocks currently in context
    seen_requests = {}    # requestId -> usage of that call
    prev_ctx = 0
    order = []            # (requestId, usage) in file order

    def add(category, label, text):
        b = Block(category, label, max(1, len(text)))
        pending.append(b)
        s.blocks.append(b)
        return b

    def call(usage, counted=True):
        nonlocal live, prev_ctx, pending
        plain = usage.get("input_tokens", 0) or 0
        cw = usage.get("cache_creation_input_tokens", 0) or 0
        cr = usage.get("cache_read_input_tokens", 0) or 0
        ctx = plain + cw + cr
        cc = usage.get("cache_creation") or {}
        w1h = cc.get("ephemeral_1h_input_tokens", 0) or 0
        write_rate = (WRITE_1H * w1h + WRITE_5M * (cw - w1h)) / cw if cw else WRITE_5M
        out = usage.get("output_tokens", 0) or 0
        call_input_w = plain + cw * write_rate + cr * READ
        s.all_weight += call_input_w + out * OUTPUT
        if not counted:
            reset = not live or (prev_ctx and ctx < prev_ctx * (1 - RESET_DROP))
            est = sum(b.chars for b in pending) / 4
            if reset:
                base = Block("Startup context", "System prompt, tools, CLAUDE.md, skills", 0,
                             tokens=max(0.0, ctx - est))
                s.blocks.append(base)
                scale = (ctx - base.tokens) / est if est else 0
                for b in pending:
                    b.tokens = b.chars / 4 * scale
                live = [base] + pending
            else:
                chars = sum(b.chars for b in pending)
                for b in pending:
                    b.tokens = (ctx - prev_ctx) * b.chars / chars if ctx > prev_ctx and chars else 0.0
                live = live + pending
            pending = []
            prev_ctx = ctx
            return
        s.calls += 1
        s.input_tokens += ctx
        s.cache_read += cr
        s.output_tokens += out
        s.thinking_tokens += (usage.get("output_tokens_details") or {}).get("thinking_tokens", 0) or 0
        s.peak_context = max(s.peak_context, ctx)

        reset = not live or (prev_ctx and ctx < prev_ctx * (1 - RESET_DROP))
        if reset:
            if live:
                s.resets += 1
            est = sum(b.chars for b in pending) / 4
            base_tokens = max(0.0, ctx - est)
            base = Block("Startup context" if not live else "Restart after compact or clear",
                         "System prompt, tools, CLAUDE.md, skills" if not live else "Summary and reloaded context",
                         0, tokens=base_tokens)
            s.blocks.append(base)
            scale = (ctx - base_tokens) / est if est else 0
            for b in pending:
                b.tokens = b.chars / 4 * scale
            live = [base] + pending
        else:
            delta = ctx - prev_ctx
            chars = sum(b.chars for b in pending)
            for b in pending:
                b.tokens = delta * b.chars / chars if delta > 0 and chars else 0.0
            live = live + pending
        new_ids = {id(b) for b in pending}
        pending = []
        prev_ctx = ctx

        total = sum(b.tokens for b in live) or 1
        shares = []
        for b in live:
            share = ctx * b.tokens / total
            b.sent += share
            shares.append(share * ((write_rate if cw else 1.0) if id(b) in new_ids else (READ if cr else 1.0)))
        # Scale so this call's input weight matches its exact usage mix.
        k = call_input_w / sum(shares) if sum(shares) else 0
        for b, w in zip(live, shares):
            b.weight += w * k
        out_w = out * OUTPUT
        out_block.weight += out_w
        out_block.sent += out
        out_block.tokens += out

    out_block = Block("Claude's output", "Replies, tool calls and thinking (output tokens)", 0)
    s.blocks.append(out_block)

    try:
        f = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return s
    with f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            t = r.get("type")
            if t == "cost-state":
                s.recorded_cost = max(s.recorded_cost, float(r.get("totalCostUSD") or 0))
                continue
            if not s.started and r.get("timestamp"):
                s.started = _ts(r["timestamp"])
            if t == "attachment":
                a = r.get("attachment") or {}
                kind = a.get("type", "")
                body = _text(a.get("content") or a.get("text") or a.get("stdout") or "")
                if kind in ("deferred_tools_delta", "deferred_tools_record"):
                    body = "\n".join(a.get("addedNames") or a.get("names") or [])
                elif kind in ("mcp_instructions_delta", "agent_listing_delta"):
                    body = "\n".join(_strings(a))
                if body.strip():
                    add("Claude Code notices", NOTICE_NAMES.get(kind, "Other notices"), body)
                continue
            m = r.get("message")
            if not isinstance(m, dict):
                continue
            if t == "assistant":
                rid = r.get("requestId") or m.get("id")
                if rid and rid not in seen_requests:
                    seen_requests[rid] = m.get("usage") or {}
                    s.model = m.get("model") or s.model
                    if m.get("usage") and m.get("model") != "<synthetic>":
                        ts = _ts(r.get("timestamp")) if since else 0
                        call(m["usage"], counted=rid not in seen and (not since or ts >= since))
                        seen.add(rid)
                for c in m.get("content") or []:
                    if not isinstance(c, dict):
                        continue
                    if c.get("type") == "tool_use":
                        tools[c.get("id")] = (c.get("name", ""), c.get("input") or {})
                        add("Claude's replies in context", "Tool calls", json.dumps(c.get("input") or {}))
                    elif c.get("type") == "text":
                        add("Claude's replies in context", "Replies", c.get("text", ""))
                    elif c.get("type") == "thinking":
                        add("Claude's replies in context", "Thinking", c.get("thinking", "") or "")
            elif t == "user":
                content = m.get("content")
                if isinstance(content, str):
                    add(_user_category(content), _short(content, 50), content)
                    continue
                for c in content or []:
                    if not isinstance(c, dict):
                        continue
                    if c.get("type") == "tool_result":
                        name, inp = tools.get(c.get("tool_use_id"), ("", {}))
                        cat, label = _tool_category(name, inp)
                        b = add(cat, label, _text(c.get("content")))
                        if name == "Read" and inp.get("file_path"):
                            s.reads[inp["file_path"]].append(b)
                    elif c.get("type") == "text":
                        text = c.get("text", "")
                        add(_user_category(text), _short(text, 50), text)
                    elif c.get("type") == "image":
                        add("Images", "Image", "x" * 6000)
    s.weight = sum(b.weight for b in s.blocks)
    return s


def _ts(v):
    try:
        return calendar.timegm(time.strptime(v[:19], "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        return 0.0


def find_sessions(root=None, days=30, project=None):
    """Yield (path, project, is_subagent) for session files changed in the last `days`."""
    root = Path(root or os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "projects"
    if not root.is_dir():
        return
    cutoff = time.time() - days * 86400 if days else 0
    for proj in sorted(root.iterdir()):
        if not proj.is_dir() or (project and project.lower() not in proj.name.lower()):
            continue
        for f in proj.rglob("*.jsonl"):
            try:
                if f.stat().st_mtime < cutoff:
                    continue
            except OSError:
                continue
            yield f, proj.name, "subagents" in f.parts


def session_rates(sessions):
    """Dollars per weight unit for each session, and how much of the total was recorded.

    Sessions where Claude Code recorded a cost get the rate that turns all their
    calls into that cost, applied to the calls in the window
    (its record includes the session's subagents, so their weight counts with
    the parent). Other sessions use the median rate of recorded sessions on the
    same model, or of all recorded sessions if that model has none.
    Returns ({session key: rate}, recorded dollars, estimated dollars).
    """
    weight, all_weight, cost, model = defaultdict(float), defaultdict(float), {}, {}
    for s in sessions:
        k = session_key(s)
        weight[k] += s.weight
        all_weight[k] += s.all_weight
        if not s.subagent:
            model[k] = s.model
            if s.recorded_cost:
                cost[k] = s.recorded_cost
    # The recorded cost covers every call in the file, including calls outside the window.
    exact = {k: cost[k] / all_weight[k] for k in cost if all_weight[k]}
    by_model = defaultdict(list)
    for k, r in exact.items():
        by_model[model.get(k, "")].append(r)
    median = lambda xs: sorted(xs)[len(xs) // 2] if xs else 0.0
    overall = median(list(exact.values()))
    rates, recorded, estimated = {}, 0.0, 0.0
    for k, w in weight.items():
        if k in exact:
            rates[k] = exact[k]
            recorded += w * exact[k]
        else:
            rates[k] = median(by_model.get(model.get(k, ""), [])) or overall
            estimated += w * rates[k]
    return rates, recorded, estimated


def session_key(s):
    return s.path.parent.parent.name if s.subagent else s.path.stem
