"""B6: repo hygiene over git-tracked content.

No 12-digit account ids, no AWS key ids, no secret-looking assignments, no
private-network addresses, no UUIDs, no absolute home-dir or protected-state
paths, and none of the banned words. All literal patterns are assembled from
fragments (as in test_banned_words) so this file passes its own scan.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from tests.test_banned_words import BANNED

REPO = Path(__file__).resolve().parents[1]

# Literal pattern strings are assembled from fragments so this test file does
# not itself trip the scan (same trick as the banned-words test).
_PRIVATE_NET = "192" + "." + "168" + "."
_HOME_PREFIX = "/ho" + "me/"
_STATE_PATH = "/ro" + "ot/.open" + "claw"

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("12-digit account id", re.compile(r"\b\d{12}\b")),
    ("AWS access key id", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    (
        "aws secret-style value",
        re.compile(r"aws_secret[a-z_]*\s*[=:]\s*['\"][A-Za-z0-9/+=]{20,}", re.I),
    ),
    ("private network address", re.compile(re.escape(_PRIVATE_NET))),
    (
        "UUID",
        re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I),
    ),
    ("protected state path", re.compile(re.escape(_STATE_PATH))),
    ("absolute home path", re.compile(re.escape(_HOME_PREFIX))),
]


def _tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files"], capture_output=True, text=True, check=True
    ).stdout.splitlines()
    return [REPO / rel for rel in out]


def test_tracked_content_is_clean() -> None:
    problems: list[str] = []
    for path in _tracked_files():
        if not path.is_file():
            continue
        content = path.read_text(errors="replace")
        rel = path.relative_to(REPO)
        for name, pattern in PATTERNS:
            if pattern.search(content):
                problems.append(f"{rel}: {name}")
        low = content.lower()
        for word in BANNED:
            if word.lower() in low:
                problems.append(f"{rel}: banned word")
    assert problems == [], "hygiene violations: " + "; ".join(problems)


def test_no_binary_fixture_dbs_committed() -> None:
    for path in _tracked_files():
        assert not path.name.endswith((".sqlite", ".db")), f"binary DB committed: {path}"
