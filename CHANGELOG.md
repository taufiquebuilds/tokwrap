# Changelog

## 0.1.1 (2026-11-04)

- Fix a crash on Windows consoles that can't print the bar characters. tokwrap now falls back to ASCII bars.

## 0.1.0 (2026-11-04)

First release.

- Reads Claude Code session logs and splits input tokens and cost by what was in the context: replies, tool output by tool, MCP servers, Claude Code notices, startup context and more.
- Prices each session from the cost Claude Code recorded, and counts each API call once across resumed sessions.
- "What to change" suggestions tied to your own numbers.
- Share card (`--card`), JSON output (`--json`), and filters by days, project or session.
