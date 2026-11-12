"""Markdown shown on GitHub: a single "~" pair strikes text through ("1~2분, 주간 5~10분").

Ranges are written with an en dash ("1–2분", "5730–5739") or inside code spans.
"""

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TILDE = re.compile(r"(?<!~)~(?!~)")


def tracked_markdown() -> list[Path]:
    out = subprocess.run(["git", "ls-files", "-z", "*.md"], cwd=ROOT, capture_output=True)
    if out.returncode != 0:
        return []
    return [ROOT / f for f in out.stdout.decode("utf-8").split("\0") if f]


def tildes(text: str) -> list[tuple[int, str]]:
    """(line number, line) for every "~" outside code blocks and inline code."""
    found, in_code = [], False
    for n, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("```"):
            in_code = not in_code
            continue
        if not in_code and TILDE.search(re.sub(r"`[^`]*`", "", line)):
            found.append((n, line.strip()))
    return found


class MarkdownTests(unittest.TestCase):
    def test_the_check_itself(self):
        self.assertEqual(tildes("일일 1~2분, 주간 5~10분"), [(1, "일일 1~2분, 주간 5~10분")])
        self.assertEqual(tildes("일일 1–2분 `a~b` ~~취소~~\n```\nx ~ y\n```"), [])

    def test_no_tilde_in_tracked_markdown(self):
        files = tracked_markdown()
        if not files:
            self.skipTest("not a git checkout")
        bad = [f"{f.relative_to(ROOT)}:{n}: {line}" for f in files
               for n, line in tildes(f.read_text(encoding="utf-8"))]
        self.assertEqual(bad, [], "write ranges with '–' (en dash) or inside code spans")


if __name__ == "__main__":
    unittest.main()
