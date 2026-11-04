# Contributing to tokwrap

The most useful contributions:

1. **New "what to change" rules** in `fixes()` in `tokwrap/cli.py`: a pattern that wastes tokens, tied to a number from the user's own data, with one concrete fix.
2. **Support for other agents' logs** (Codex CLI first): a parser that produces the same `Session` and `Block` objects as `tokwrap/analyze.py`.
3. **Better attribution** for anything the report puts in the wrong place. Include a small, anonymised log excerpt.

## Rules

- Standard library only. tokwrap must install and run anywhere Python 3.9 does.
- It only reads files. Never upload, write to `~/.claude`, or change settings.
- Every number in a suggestion comes from the user's data. No made-up savings.

## Before you open a pull request

```sh
python3 -m unittest discover -s tests -v
python3 -m tokwrap --session tests/fixtures/session.jsonl
```

By contributing, you agree that your contributions are licensed under the MIT License.
