# Guide for agents working on this repo

tokwrap is a standard-library Python CLI. `tokwrap/analyze.py` parses Claude Code logs into sessions and priced blocks, `tokwrap/cli.py` summarises and prints, `tokwrap/card.py` draws the share card.

- No dependencies. Python 3.9+.
- Read-only: never write to `~/.claude` or upload anything.
- Totals must stay exact: the sum of every block's sent tokens equals the input plus output tokens in the logs. A test checks this.
- Run `python3 -m unittest discover -s tests` before committing.
