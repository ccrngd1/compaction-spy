# CompactionSpy

An **offline, read-only** provenance auditor for OpenClaw context-compaction
summaries. OpenClaw compacts long agent sessions into a five-section summary
("safeguard" format) and drops the raw history. CompactionSpy takes a snapshot
of an agent's SQLite transcript store, finds every compaction event, and checks
each summary entry against the raw history it replaced: did the entry come from
a tool output, a user turn, the previous summary, the kept tail, only the
agent's own prose — or does no source exist at all?

**It measures provenance, not truth.** SJC (the paper this builds on) found
that entries without a source are correct at nearly the same rate as entries
with one (90.6% vs 92.2%). "No source found" means "the compressor wrote it",
not "it is false".

## Credit and framing

The method builds directly on **SJC** — *"Prune the Summary, Not the History:
Source-Judged Compaction for Long-Horizon Agents"*, Min et al., arXiv
2610.20927 (Oct 2026). SJC asks the same source question online, at compaction
time, on single-instruction benchmark tasks, with the compressor being
OpenClaw's own safeguard compaction. SJC explicitly lists multi-turn settings —
where earlier user messages are themselves a legitimate source — as untested.
CompactionSpy is **the multi-turn production audit SJC lists as untested**: an
offline audit of real operator transcripts with typed sources (TOOL / USER /
PREV / TAIL / agent-only) and segment awareness (summarized span vs kept tail).

No real-data results are claimed here: this is a new build, validated by an
offline test suite against synthetic fixtures. The real-data acceptance run is
pending and will be done separately by the operator.

## Install

Not published to any package registry. Install from source:

```bash
git clone https://github.com/ccrngd1/compaction-spy.git && cd compaction-spy && python3 -m venv .venv && .venv/bin/pip install -e .
```

Requires Python >= 3.11. Runtime dependencies (pinned in `pyproject.toml`):
boto3, zstandard, rank-bm25, typer, rich. Dev extras: `pip install -e '.[dev]'`
(pytest, ruff, mypy).

## Snapshot procedure

The tool never opens the live store for auditing. First take a consistent,
read-only snapshot into a private directory:

```bash
compaction-spy snapshot --agent <agent-id> --to /path/to/private/snapshots
```

- Resolves `<state-dir>/agents/<agent-id>/agent/openclaw-agent.sqlite`, opens
  it with a `mode=ro` SQLite URI, and copies it with the sqlite3 **backup
  API** (safe against a live WAL database), into a temp file in the
  destination dir.
- Runs `PRAGMA integrity_check`, then atomically renames into place. On a
  busy/locked source or an integrity failure the partial file is removed and
  the command exits non-zero with a clear message — no partial copies.
- The destination dir is `chmod 700`. Writing the snapshot (or any other
  output) under `~/.openclaw` (or `$OPENCLAW_STATE_DIR`) is refused, with
  symlinks resolved first.
- A marker sidecar (`<name>.compaction-spy-snapshot.json`) is written next to
  each snapshot. `audit --cleanup` (the default) deletes **only** DB files
  carrying that marker, and only after a successful run — never arbitrary
  files, never after an abort.
- `--source <path>` overrides the agent-id resolution (used by tests to
  snapshot a fixture).

## Commands

```bash
compaction-spy census --db snap1.sqlite --db snap2.sqlite [--json]
compaction-spy audit --db snap.sqlite --out /private/outdir \
    [--agent NAME] [--since ISO] [--limit N] [--max-usd X] [--judge bedrock|stub] \
    [--no-fullctx] [--include-text] [--cleanup/--no-cleanup]
compaction-spy memory --memory-md MEMORY.md --git-restore /path/to/repo [--sample N]
```

- **census** counts compactions by store / format (safeguard vs legacy) /
  month, deduplicated by summary hash. Malformed DBs are skipped and reported.
  Legacy free-form summaries are counted as unparsed, not audited.
- **audit** runs the full pipeline: serial event decode → case building
  (summarized span vs kept tail at `firstKeptEntryId`) → free deterministic
  identifier check → BM25 retrieval judge (one request per compaction, 70k-char
  evidence budget) → full-context confirmation of non-sourced entries (4k chars
  per message, 420k total, earliest truncated first). `census` and `audit`
  refuse to read a `--db` under the protected state dir — snapshot first.
- **memory** is a secondary mode: MEMORY.md bullets vs the agent's daily logs,
  restored from git history into a temp dir that is always deleted on exit.
  Its output is **self-consistency, not ground truth** — both files are
  agent-written, so it cannot establish ground truth, only drift.

## Labels and flags

| Label | Meaning |
|---|---|
| `SOURCED:TOOL` | A tool output establishes the entry |
| `SOURCED:USER` | A user turn establishes it (principal sub-tag `cc\|cron\|system\|agent\|unknown`; `unknown` in v0.1) |
| `SOURCED:PREV` | The previous summary establishes it |
| `SOURCED:TAIL` | Only the kept tail establishes it |
| `AGENT_ONLY` | Only the agent's own prose or tool-call arguments support it |
| `UNSUPPORTED` | Nothing in the history establishes it — no source found |
| `CONTRADICTED` | The summarized history or previous summary refutes it |

| Flag | Meaning |
|---|---|
| `STALE` | Open at the cut, completed in the kept tail — reported separately, a risk, not an error |
| `ID_UNVERIFIED` | A backtick/URL identifier in the summary is not found verbatim in the raw history |

Judge output is validated strictly against this schema; invalid output is
retried with backoff and then counted in a judge-error bucket (never silently
dropped). The tool's reports always say "no source found" for unsourced
entries — never stronger words.

## Privacy posture

- Default `report.md` / `report.json` contain **aggregates plus opaque entry
  refs only**: `store/session-hash/compaction-id/section/index`. Session ids
  are hashed. No entry text and no judge rationales (rationales echo private
  content).
- `--include-text` writes `entries.private.jsonl` (entry text + rationales)
  **only** inside the private `--out` dir, for operator review.
- All test fixtures are synthetic; no production text anywhere in the repo.

## Cost guard

- Judge: Bedrock only, region `us-east-1`, model
  `global.anthropic.claude-sonnet-4-6`, ambient credentials. No account ids,
  ARNs or keys in code.
- `--max-usd` is a hard ceiling, enforced at three checkpoints. All estimates
  use request character counts (chars/4 ≈ tokens, default $3/M input, $15/M
  output — configurable via `--price-input`/`--price-output`) plus worst-case
  output tokens per call.
  - **Stage-1 pre-run estimate** covers the retrieval stage only (one request
    per compaction — those prompts are all built up front); if it exceeds the
    budget, the run aborts before spending anything. The full-context stage
    cannot be estimated at this point because it depends on which entries the
    retrieval judge flags.
  - **Stage-2 pre-check:** after the retrieval stage finishes and before any
    full-context call, all full-context prompts for the flagged entries are
    built and estimated; if actual spend so far plus that estimate exceeds the
    budget, the run aborts with no full-context requests made. `--no-fullctx`
    skips the stage and the check.
  - **Mid-run guard:** the running total from reported usage is checked before
    each call; a potential overrun cancels pending calls and aborts.

  Both stage estimates are recorded in the report's cost section. Any abort
  exits non-zero with a clear message naming the stage and writes **no report
  files**.
- `--judge stub` is a deterministic offline judge for tests and demos. It makes
  no network calls and deliberately echoes entry text in its rationales so the
  privacy tests can prove rationales are stripped from default reports.

## Design notes (ambiguities and the choices made)

- **Entry index in opaque refs** is the position within the compaction's full
  parsed entry list (stable across runs), not per-section.
- **Mid-run abort granularity:** the guard checks before each judge call using
  the call's worst-case cost (input estimate + max output tokens). Pending
  futures are cancelled; in-flight calls finish but their results are
  discarded along with all report output.
- **`SOURCED:USER` principal sub-tag** is always `unknown` in v0.1 (allowed by
  the requirements); the PolicyGate classifier is not wired in.
- **Dedupe scope:** census dedupes by summary hash across all given stores, so
  the same summary copied into two sessions or stores is audited once (first
  store wins).
- **`--since`** compares ISO-8601 timestamp strings lexicographically against
  the compaction event's `timestamp`.
- **Memory-mode judge transport** reuses the audit judge clients with a
  separate response schema (`SUPPORTED/PARTIAL/NO_EVIDENCE/CONTRADICTED`);
  unparseable responses are counted as unlabelled rather than retried into the
  audit schema.
- **Snapshot cleanup markers:** `--cleanup` only ever deletes files with this
  tool's marker sidecar, so pointing `audit` at a hand-made DB copy without a
  marker leaves it untouched.

## Caveats

- **Provenance ≠ truth** (see above). Headline "% unsourced" figures require a
  human precision check first; until then treat flagged entries as a review
  queue.
- Spike percentages from the research phase are **not** findings of this tool;
  the real-data acceptance run is pending.
- Retrieval false negatives are unmeasured: the full-context stage only
  re-checks entries the retrieval pass flagged.

## Future work

- `openclaw sessions export-trajectory` as an alternative, snapshot-free input
  path.
- Stretch flags: `USER_ATTRIBUTED_TOOL_SOURCED` (laundering heuristic, after
  arXiv 2610.29869) and `MISSING` (recall: a user ask with no summary entry).
- Embeddings for paraphrase-heavy sections, only if a gold-set check shows a
  recall gap.
