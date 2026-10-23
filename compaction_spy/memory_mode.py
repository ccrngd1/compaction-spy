"""Secondary MEMORY.md self-consistency mode (ported from ``spike_memorymd.py``).

Checks MEMORY.md bullets against the agent's daily logs. Both files are
agent-written, so this measures SELF-CONSISTENCY, NOT GROUND TRUTH — the output
is labelled accordingly. Daily logs pruned from the working tree are restored
from git history into a temp dir that is always deleted on exit (also on
error), per the research doc's restore procedure.
"""

from __future__ import annotations

import datetime
import json
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from rank_bm25 import BM25Okapi

from .judge import JudgeClient, JudgeUsage
from .retrieval import tokenize

MEMORY_SYSTEM = """You verify one claim from an AI agent's long-term memory file (MEMORY.md) against excerpts of the agent's daily log files (memory/YYYY-MM-DD.md). Both were written by the agent.
Labels: SUPPORTED (a log excerpt records the same event/decision/fact), PARTIAL (some material parts supported, others not found), NO_EVIDENCE (excerpts do not mention it; it may come from elsewhere), CONTRADICTED (an excerpt records the opposite or a different value/date).
Also: claim_type = EVENT (something happened on a date), DECISION, RULE/PREFERENCE, STATE (current status), LESSON/PRINCIPLE.
Return ONLY JSON {"label":"...","claim_type":"...","conf":0-1,"evidence_date":"YYYY-MM-DD or null","why":"<=25 words"}"""

SELF_CONSISTENCY_NOTE = (
    "MEMORY.md mode measures self-consistency, not ground truth: both MEMORY.md "
    "and the daily logs are agent-written."
)

_DATE = re.compile(r"20\d\d-\d\d-\d\d")
_LOG_PATH = re.compile(r"memory/20[0-9-]+\.md$")


@dataclass
class MemoryClaim:
    section: str
    text: str


def parse_claims(memory_md: str, min_len: int = 40) -> list[MemoryClaim]:
    claims: list[MemoryClaim] = []
    section = ""
    for line in memory_md.splitlines():
        if line.startswith("#"):
            section = line.strip("# ").strip()
            continue
        s = line.strip()
        if re.match(r"^[-*]\s+", s) and len(s) > min_len:
            claims.append(MemoryClaim(section=section, text=re.sub(r"^[-*]\s+", "", s)))
    return claims


@contextmanager
def restored_daily_logs(repo: Path, temp_parent: Path | None = None) -> Iterator[Path]:
    """Restore all daily logs (working tree + git history) into a temp dir.

    The temp dir is deleted on exit, including on error — the restored logs
    are private and must not persist.
    """
    tmp = Path(tempfile.mkdtemp(prefix="cspy-daily-", dir=temp_parent))
    tmp.chmod(0o700)
    try:
        listed = subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "log",
                "--all",
                "--name-only",
                "--format=",
                "--",
                "workspace/memory/20*.md",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        paths = sorted({p for p in listed.splitlines() if _LOG_PATH.search(p)})
        for rel in paths:
            name = Path(rel).name
            working = repo / rel
            if working.is_file():
                shutil.copy(working, tmp / name)
                continue
            commit = subprocess.run(
                ["git", "-C", str(repo), "rev-list", "-n1", "HEAD", "--", rel],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            if not commit:
                continue
            shown = subprocess.run(
                ["git", "-C", str(repo), "show", f"{commit}^:{rel}"],
                capture_output=True,
                text=True,
                check=False,
            )
            if shown.returncode == 0:
                (tmp / name).write_text(shown.stdout)
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def build_log_chunks(logs_dir: Path, chunk: int = 1200) -> list[tuple[str, str]]:
    docs: list[tuple[str, str]] = []
    for f in sorted(logs_dir.glob("20*.md")):
        date = f.name[:10]
        text = f.read_text(errors="replace")
        paras = [p for p in re.split(r"\n\s*\n|\n(?=#)", text) if p.strip()]
        for p in paras:
            for i in range(0, len(p), chunk):
                docs.append((date, p[i : i + chunk]))
    return docs


def check_claim(
    claim: MemoryClaim,
    docs: list[tuple[str, str]],
    bm: BM25Okapi,
    judge: JudgeClient,
    top_k: int = 8,
) -> tuple[dict[str, object], JudgeUsage]:
    scores = bm.get_scores(tokenize(claim.text))
    dates = _DATE.findall(claim.text)
    if dates:
        try:
            refs = [datetime.date.fromisoformat(d) for d in dates]
            for i, (d, _t) in enumerate(docs):
                dd = datetime.date.fromisoformat(d)
                if min(abs((dd - r).days) for r in refs) <= 7:
                    scores[i] *= 1.5
        except ValueError:
            pass
    top = sorted(range(len(docs)), key=lambda i: -scores[i])[:top_k]
    ev = "\n\n".join(f'<log date="{docs[i][0]}">\n{docs[i][1]}\n</log>' for i in top)
    user = (
        f'<claim section="{claim.section}">{claim.text}</claim>\n\n'
        f"<log_excerpts>\n{ev}\n</log_excerpts>"
    )
    text, usage = judge.complete(MEMORY_SYSTEM, user, 400)
    start, end = text.find("{"), text.rfind("}")
    try:
        parsed = json.loads(text[start : end + 1]) if start >= 0 else {}
    except json.JSONDecodeError:
        parsed = {}
    label = parsed.get("label")
    if label not in {"SUPPORTED", "PARTIAL", "NO_EVIDENCE", "CONTRADICTED"}:
        label = None
    return (
        {
            "label": label,
            "claim_type": parsed.get("claim_type"),
            "conf": parsed.get("conf"),
            "dated": bool(dates),
        },
        usage,
    )
