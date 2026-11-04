"""Run with: python3 -m unittest discover -s tests"""

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIX = ROOT / "tests" / "fixtures" / "session.jsonl"

from tokwrap.analyze import parse, session_rates  # noqa: E402
from tokwrap.cli import fixes, summarize  # noqa: E402
from tokwrap.card import headline, svg  # noqa: E402


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.s = parse(FIX, "proj")

    def test_totals_match_usage(self):
        self.assertEqual(self.s.calls, 3)
        self.assertEqual(self.s.input_tokens, 10000 + 12000 + 12500)
        self.assertEqual(self.s.output_tokens, 200)
        self.assertEqual(self.s.recorded_cost, 1.5)

    def test_sent_tokens_add_up(self):
        sent = sum(b.sent for b in self.s.blocks)
        self.assertAlmostEqual(sent, self.s.input_tokens + self.s.output_tokens, delta=1)

    def test_growth_is_attributed_to_the_new_block(self):
        bash = next(b for b in self.s.blocks if b.category == "Shell output")
        read = next(b for b in self.s.blocks if b.category == "File reads")
        # The Bash result arrived together with the Read tool call, so it gets most of the 2,000.
        self.assertGreater(bash.tokens, 1900)
        self.assertLessEqual(bash.tokens, 2000)
        # It was carried by calls 2 and 3.
        self.assertAlmostEqual(bash.sent, bash.tokens * 2, delta=bash.tokens * 0.05)
        self.assertLessEqual(read.tokens, 500)

    def test_startup_is_the_first_context(self):
        start = next(b for b in self.s.blocks if b.category == "Startup context")
        self.assertGreater(start.tokens, 9900)
        self.assertAlmostEqual(start.sent, start.tokens * 3, delta=start.tokens * 0.05)

    def test_resumed_copy_is_not_counted_twice(self):
        seen = set()
        first = parse(FIX, "proj", seen)
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "copy.jsonl"
            shutil.copy(FIX, copy)
            second = parse(copy, "proj", seen)
        self.assertEqual(first.calls, 3)
        self.assertEqual(second.calls, 0)
        self.assertEqual(second.input_tokens, 0)

    def test_recorded_cost_is_used_exactly(self):
        rates, recorded, estimated = session_rates([self.s])
        self.assertEqual(recorded, 1.5)
        self.assertEqual(estimated, 0)
        r = summarize([self.s])
        self.assertAlmostEqual(sum(usd for _, _, usd, _ in r["categories"]), 1.5, places=6)

    def test_calls_before_the_window_are_not_counted(self):
        s = parse(FIX, "proj", since=2e9)  # a date after every call in the fixture
        self.assertEqual(s.calls, 0)
        self.assertEqual(s.input_tokens, 0)

    def test_bad_lines_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "bad.jsonl"
            p.write_text("not json\n" + FIX.read_text() + "{broken\n")
            self.assertEqual(parse(p).calls, 3)


class ReportTests(unittest.TestCase):
    def test_summary_fixes_and_card(self):
        r = summarize([parse(FIX, "proj")])
        self.assertEqual(r["categories"][0][0], "Startup context")
        self.assertIsInstance(fixes(r), list)
        self.assertTrue(headline(r).startswith(f"{r['categories'][0][1]:.0%} of my Claude Code spend"))
        self.assertIn("<svg", svg(r, "test"))

    def test_cli_json_on_one_session(self):
        out = subprocess.run([sys.executable, "-m", "tokwrap", "--session", str(FIX), "--json"],
                             cwd=ROOT, capture_output=True, text=True, check=True)
        data = json.loads(out.stdout)
        self.assertEqual(data["calls"], 3)
        self.assertAlmostEqual(data["cost"], 1.5, places=6)

    def test_cli_report_on_an_ascii_console(self):
        env = dict(__import__("os").environ, PYTHONIOENCODING="cp1252")
        out = subprocess.run([sys.executable, "-m", "tokwrap", "--session", str(FIX)],
                             cwd=ROOT, capture_output=True, text=True, encoding="cp1252", env=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("Where it went", out.stdout)

    def test_cli_report_on_one_session(self):
        out = subprocess.run([sys.executable, "-m", "tokwrap", "--session", str(FIX)],
                             cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("Where it went", out.stdout)
        self.assertIn("$1.50", out.stdout)


if __name__ == "__main__":
    unittest.main()
