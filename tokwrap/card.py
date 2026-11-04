"""A 1200x630 share card: the headline finding and the top categories."""

import shutil
import subprocess
import tempfile
from html import escape
from pathlib import Path

BG, FG, MUTED, FAINT, ACCENT = "#f6f4ef", "#1b1b1b", "#6f6a61", "#e2ddd2", "#d9480f"

PHRASES = {
    "Claude's replies in context": "re-sent Claude's own earlier work",
    "Claude Code notices": "went on Claude Code's notices: tool lists, MCP, hooks",
    "Claude's output": "was Claude's output",
    "Shell output": "re-sent shell output",
    "File reads": "re-sent files it had read",
    "Subagents (their own context)": "went on subagents",
    "Startup context": "went on the startup context sent with every call",
    "Restart after compact or clear": "went on summaries after /compact",
    "Your messages": "re-sent my own messages",
    "Skill instructions": "went on skill instructions",
}
CHROMES = ["google-chrome", "chromium", "chromium-browser", "microsoft-edge",
           "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
           "/Applications/Chromium.app/Contents/MacOS/Chromium",
           "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]


def headline(r):
    name, p, _, _ = r["categories"][0]
    what = "spend" if r["has_cost"] else "tokens"
    return f"{p:.0%} of my Claude Code {what} {PHRASES.get(name, 'went on ' + name.lower())}"


def _wrap(text, width):
    lines, line = [], ""
    for word in text.split():
        if line and len(line) + 1 + len(word) > width:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    return lines + [line]


def svg(r, title):
    from .cli import fmt_tokens, fmt_usd
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630" viewBox="0 0 1200 630">',
           f'<rect width="1200" height="630" fill="{BG}"/>',
           '<style>text{font-family:"Helvetica Neue",Helvetica,Arial,sans-serif}</style>',
           f'<text x="64" y="84" font-size="22" font-weight="700" fill="{MUTED}" letter-spacing="2">'
           f'TOKWRAP · {escape(title.upper())}</text>']
    y = 150
    lines = _wrap(headline(r), 40)[:3]
    for line in lines:
        out.append(f'<text x="64" y="{y}" font-size="46" font-weight="700" fill="{FG}">{escape(line)}</text>')
        y += 56
    stats = (f"{fmt_usd(r['cost'])} at API prices · " if r["has_cost"] else "") + \
            f"{fmt_tokens(r['input_tokens'])} input tokens · {r['sessions']} sessions"
    out.append(f'<text x="64" y="{y + 6}" font-size="24" fill="{MUTED}">{escape(stats)}</text>')

    top = [c for c in r["categories"] if c[1] >= 0.01][:5 if len(lines) < 3 else 4]
    bar_y, max_w = y + 50, 640
    biggest = top[0][1] if top else 1
    for i, (name, p, usd, _) in enumerate(top):
        by = bar_y + i * 52
        w = max(4, max_w * p / biggest)
        out.append(f'<text x="64" y="{by + 26}" font-size="22" fill="{FG}">{escape(name)}</text>')
        out.append(f'<rect x="440" y="{by + 6}" width="{w:.0f}" height="28" fill="{ACCENT if i == 0 else FAINT}"/>')
        out.append(f'<text x="{440 + w + 12:.0f}" y="{by + 28}" font-size="22" font-weight="{700 if i == 0 else 400}"'
                   f' fill="{FG}">{p:.0%}</text>')
    out.append(f'<text x="1136" y="600" font-size="20" fill="{MUTED}" text-anchor="end">'
               'github.com/taufiquebuilds/tokwrap</text>')
    out.append("</svg>")
    return "\n".join(out)


def write_card(r, title, path):
    path = Path(path)
    svg_path = path.with_suffix(".svg")
    svg_path.write_text(svg(r, title), encoding="utf-8")
    written = [svg_path]
    chrome = next((c for c in CHROMES if shutil.which(c) or Path(c).exists()), None)
    if chrome:
        png = path.with_suffix(".png")
        with tempfile.TemporaryDirectory() as tmp:
            page = Path(tmp) / "card.html"
            page.write_text(f'<html><body style="margin:0">{svg_path.read_text(encoding="utf-8")}</body></html>',
                            encoding="utf-8")
            try:
                subprocess.run([chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                                f"--screenshot={png.resolve()}", "--window-size=1200,630",
                                "--force-device-scale-factor=2", page.as_uri()],
                               capture_output=True, timeout=60, check=False)
            except (OSError, subprocess.TimeoutExpired):
                pass
        if png.exists():
            written.append(png)
    return written
