# Checking My AI Agents' Compressed Memory Against the Raw Transcripts

*A paper tied OpenClaw's safeguard compressor to 29 of 35 failed benchmark runs. I run OpenClaw agents in production. So I built a provenance auditor for my own compaction summaries.*

**What this project is:** A read-only CLI that audits OpenClaw context-compaction summaries against the raw transcripts they replaced, checking where each summary entry actually came from.
**The problem it solves:** When agents compress long sessions into summaries, entries can appear that have no source in the original conversation. Those entries drive future agent actions. No tool existed to check this on real production data.
**The key insight:** The compressor almost never gets identifiers wrong (99.3% appear verbatim in the history). Where it goes off-script is in to-do items and plans, and those are the entries most likely to drive agent behavior.

---

## The Paper That Started This

In October 2026, Min et al. at the University of Virginia published *"Prune the Summary, Not the History: Source-Judged Compaction for Long-Horizon Agents"* ([SJC, arXiv 2610.20927](https://arxiv.org/abs/2610.20927)). Their method asks a straightforward question: for each entry in a compaction summary, can you find a source for it in the history the summary replaced?

Their headline finding, on AppWorld (90 tasks × 3 runs, GPT-5.6 Luna as both agent and compressor): **29 of 35 failed runs (83%) traced back to an action that carried out a specific summary entry.** Of those 29 entries, 22 had no source in the environment output and 3 contradicted one. Only 4 were actually supported.

Here's the part that made me stop scrolling: **the compressor they tested was OpenClaw's own safeguard compaction.** The structured five-section summarizer that my agents run every day. They ported it directly from the OpenClaw source into [`sjc/safeguard.py`](https://github.com/GuanghuiMin/sjc).

SJC also found the problem isn't evenly distributed. In their AppWorld runs, 67.7% of Open TODO entries and 86.1% of Pending User Ask entries had no environment source. Exact Identifiers were much cleaner at 3.4%. The plan-like sections are where the compressor goes off-script.

One caveat SJC is upfront about, and it matters: entries without a source are correct at nearly the same rate as entries with one (90.6% vs 92.2%). "No source found" means "the compressor wrote it." It does not mean "it is false." CompactionSpy measures **provenance, not truth.** That distinction matters for everything that follows.

SJC explicitly listed multi-turn settings, where earlier user messages are themselves a legitimate source, as untested. I run 10 OpenClaw agents. Eight months of production sessions. 49 structured compaction events across four agent stores. So I built the multi-turn production audit SJC left as future work.

## How Compaction Works

When an OpenClaw session gets long enough, the framework compresses the conversation. It takes the full history, generates a structured summary with five sections (Exact Identifiers, Constraints/Rules, Decisions Made, Open TODOs, Pending User Asks), and drops the original messages. Everything after a cut point (the "kept tail") stays as-is. The summary plus the tail become the agent's working memory going forward.

This is "summarize-and-replace" compaction, and it's everywhere: OpenClaw, Hermes, Claude Code, Gemini CLI, LangChain, OpenHands all do some version of it. Microsoft's CompactionBench study found that only 7.8% of Copilot sessions compact, but those sessions account for 44.2% of all tokens. The sessions that compact are the long, complex ones where memory fidelity matters most.

The problem is the summarizer is an LLM. It generates new text. Sometimes that text isn't grounded in the input. In the compaction context, a summary entry might describe a constraint that doesn't exist, a to-do item that was already finished, or a decision that was never made. The agent then treats that entry as fact.

This isn't hypothetical. Hermes [#41607](https://github.com/NousResearch/hermes-agent/issues/41607) (P1) documented stale items from the summary's Pending User Asks being executed as the current task, even with a "REFERENCE ONLY" preamble. Claude Code [#24792](https://github.com/anthropics/claude-code/issues/24792) reported destructive compaction leading to wrong next actions. OptionWorld ([arXiv 2610.28491](https://arxiv.org/abs/2610.28491)) found a related pattern from the other direction: in 35.5% of failures, key facts were present in the agent's input but the agent didn't act on them. Different mechanism (execution failure, not compaction), but the same takeaway: wrong or stale notes in an agent's memory change its behavior.

## What I Built

CompactionSpy is an offline, read-only Python CLI. It never opens the live agent store. You take a consistent snapshot of the SQLite transcript database, and the tool audits the compaction summaries it finds.

The pipeline has three stages, and the order matters.

**Stage 1: Deterministic identifier check (free).** Every backticked token and URL in the summary gets a verbatim search against the raw history events. This costs nothing, takes milliseconds, and gives you a clean baseline. In my corpus, **99.3% of identifier tokens (2,001 of 2,016) appeared verbatim in the history.** The compressor is very good at preserving exact strings. The 15 that didn't match were scattered across all four agent stores: altered paths, shortened URLs, formatting changes. This check is deterministic. No LLM involved. I trust this number.

**Stage 2: BM25 retrieval judge.** For each compaction, the tool builds a case from the history span the summary replaced. It splits the span into the summarized portion (before the cut) and the kept tail (after the cut), types each message as USER, TOOL, or ASSISTANT, and retrieves evidence using BM25 over typed windows with a 70k-character evidence budget. One Sonnet 4.6 request per compaction classifies every entry with a provenance label.

SJC uses its own judge model (Jev-1.13, served via OpenRouter, AUROC 0.905). Jev is calibrated and cheap, but it's not available on Bedrock. So CompactionSpy uses Sonnet 4.6 at temperature 0, which shows 94.5% binary agreement with itself across two runs. Haiku 4.5 was tested and rejected: it resolved 56.5% of Sonnet's flags as sourced. Not acceptable for an auditor.

**Stage 3: Full-context confirmation.** This is the critical stage. Stage 2 over-flags. In the acceptance run, **48% of the entries Stage 2 flagged were actually sourced once the judge saw the full history** instead of just the top BM25 hits. So every flagged entry gets a second pass with the complete conversation visible (capped at 420k characters, truncating oldest messages first). Only entries that survive both passes count.

If you skip this stage (`--no-fullctx`), your numbers will be nearly 2x higher, and most of that difference will be your auditor's retrieval misses, not your compressor's problems.

The labels tell you where an entry came from:

| Label | What it means |
|---|---|
| `SOURCED:TOOL` | A tool output establishes the entry |
| `SOURCED:USER` | A user turn establishes it |
| `SOURCED:PREV` | The previous summary establishes it |
| `SOURCED:TAIL` | Only the kept tail establishes it |
| `AGENT_ONLY` | Only the agent's own reasoning supports it |
| `UNSUPPORTED` | Nothing in the history establishes it |
| `CONTRADICTED` | The history refutes it |

There's also a `STALE` flag for entries listed as open in the summary but completed in the kept tail. The summary says "still to do: X" while the tail shows X done. That conflict is the exact pattern behind Hermes #41607.

### Install and run

Not on PyPI. Install from source:

```bash
git clone https://github.com/ccrngd1/compaction-spy
cd compaction-spy
pip install -e .
```

```bash
# Snapshot the store (read-only, never touches the live DB)
compaction-spy snapshot --agent techwriter --to /tmp/cspy-private

# What's in there?
compaction-spy census --db /tmp/cspy-private/techwriter.sqlite

# Run the audit with a $15 hard cap
compaction-spy audit --db /tmp/cspy-private/techwriter.sqlite \
    --max-usd 15 --out /tmp/cspy-private/results
```

The `--max-usd` flag is a real ceiling: the tool estimates cost before making any Bedrock calls and aborts cleanly if it would exceed the budget.

## Early Results (Flags Pending Human Review)

**Caveat up front.** Every number below comes from an LLM judge (Sonnet 4.6). No human has verified the flags yet. A precision check is pending: **[A3: precision TBD% (95% CI TBD)].** Until that check is done, treat these as "what the tool flagged," not "what is true." If precision comes back below 70%, all of this gets reframed as a review queue and the percentages get discounted accordingly. And again: provenance is not truth. An entry flagged as unsourced might be perfectly correct. These flags say "no source found," never anything stronger.

**The corpus.** 49 safeguard-format compactions across 4 agent stores, containing 1,959 parsed entries. The corpus is small and skewed: 38 of 49 compactions come from one agent running one recurring cron workflow. September dominates. Per-agent comparisons are directional, not definitive.

**What the tool flagged.** After the full-context confirmation pass, the tool flagged 8.1% of entries (158 of 1,959) as having no source in the history. Of those, 53 (2.7%) were flagged as contradicted. The tool also flagged 268 entries as stale.

**Section breakdown (all flags pending review):**

| Section | Entries | Flagged no-source | SJC AppWorld rate |
|---|---|---|---|
| Open TODOs | 242 | 24.0% | 67.7% |
| Pending user asks | 115 | 20.0% | 86.1% |
| Decisions | 271 | 7.4% | 21.0% |
| Exact identifiers | 994 | 4.7% | 3.4% |
| Constraints/Rules | 337 | 3.0% | 11.1% |

The shape matches SJC: plan-like sections are flagged most often, identifiers and constraints are the cleanest. But the absolute rates are lower, which makes sense. SJC studied single-instruction tasks where the only legitimate source is environment output. My agents are multi-turn: user messages are a real source. In the retrieval pass, roughly half of Open TODO entries and three-quarters of Pending User Ask entries traced to a USER turn. SJC intentionally excludes that. Different settings, different baselines.

**Stale items (flags pending review).** The tool flagged 63% of Open TODO entries (153 of 242) and 74% of Pending User Ask entries (85 of 115) as stale: listed as open in the summary but completed in the kept tail. Whether the agent acts on the stale item depends on how it resolves the conflict, but the conflict shouldn't be there.

**Cron vs. interactive (directional, small sample).** The cron agent (one recurring workflow, small scope) showed about 6% flags. The interactive agents (multi-turn conversations, more decisions, more plans) showed 15-18%. (The research agent, which runs short scoped sessions, came in at about 2%.) SJC explicitly listed multi-turn as untested, and this may be what untested multi-turn looks like in production: more decisions and plans mean more compressor-generated reasoning, and more flags. But 9 interactive compactions across two stores isn't enough to draw conclusions from.

**Cost.** $10.43 for the full 49-compaction corpus. About $0.21 per compaction. A nightly audit of new compactions would run under $10 a month.

## The Honest Part

Three things to be direct about.

**The auditor itself is the biggest source of noise.** 48% of Stage 2 flags turned out to be sourced once Stage 3 saw more context. Single-pass retrieval auditing overstates the problem by nearly 2x. If you build something similar, do not skip the confirmation stage.

**An LLM is judging LLM output.** Sonnet judging Claude-authored summaries may be biased toward calling things sourced (same model family, similar reasoning). A cross-family judge or a calibrated specialist model like SJC's Jev would be a better test. That's future work.

**The corpus is small and weird.** One cron agent dominates. September dominates. The interactive cases, where the interesting patterns show up, are underrepresented. CompactionSpy is designed to re-run monthly as compactions accrue. The numbers will get more meaningful over time.

## What I'd Change in My Setup

Three concrete changes, based on what the tool flagged and what the literature supports.

**Pin constraints.** The Governance Decay paper ([arXiv 2606.22528](https://arxiv.org/abs/2606.22528)) showed constraint violations going from 0% to 30% after compaction. "Constraint Pinning," keeping the constraint visible in a prefix that survives compaction, brings violations back to 0%. My Constraints/Rules section is the cleanest at 3.0% flagged, but even a few unsourced constraints could change agent behavior. Moving critical rules to a pinned section is cheap insurance.

**Tag unsourced plans at compaction time.** SJC's approach prunes entries below a source-probability threshold for under $0.002 per task. A gentler version: prefix flagged entries with `[unverified]` so the agent can weight them lower without losing the information entirely. CompactionSpy's pipeline is the detection half of that workflow.

**Re-check open items against the tail before writing the summary.** The stale pattern is the simplest to fix. Before the compressor writes "Open TODO: X," check the most recent messages for evidence that X was completed. If it was, mark it done. This only needs the kept tail, not the full history, so it's much cheaper than a full provenance audit.

## Try It

CompactionSpy is open source. Not on PyPI: install from the repo.

```bash
git clone https://github.com/ccrngd1/compaction-spy
cd compaction-spy
pip install -e .
```

Requires Python >= 3.11 and AWS Bedrock access (Sonnet 4.6, ambient credentials). The full audit of 49 compactions cost $10.43. The `--max-usd` flag keeps you in budget.

The tool measures provenance, not truth. It tells you where your compaction entries came from. What you do with that is up to you.

If you're running OpenClaw or any agent framework with context compaction, I'd be curious what your numbers look like.

**Credit where it's due.** CompactionSpy builds directly on **SJC** by Min et al. ([arXiv 2610.20927](https://arxiv.org/abs/2610.20927)). SJC asks the source question online, at compaction time, on single-instruction benchmark tasks, using OpenClaw's safeguard compressor. CompactionSpy is the offline, multi-turn, production-data audit that SJC explicitly lists as future work. Everything I know about this problem started with their paper.

---

*CompactionSpy is a [Protogenesis](https://github.com/ccrngd1) build (W43). Repo: [github.com/ccrngd1/compaction-spy](https://github.com/ccrngd1/compaction-spy)*
