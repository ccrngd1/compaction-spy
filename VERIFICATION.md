# VERIFICATION

What the offline test suite proves, versus what only the real-data acceptance
run can prove. All tests are hermetic: AWS credentials are faked, boto3 client
construction is blocked, and every fixture is synthetic.

## Proven offline (build-time criteria)

| Criterion | What is proven | Test file / tests |
|---|---|---|
| B1: section parser | 5-section parsing, continuation lines joined, none/n/a skipped, numbered/star bullets, unknown sections ignored, legacy detection | `tests/test_parse.py` (all tests) |
| B1: TAIL segmentation | Split at `firstKeptEntryId`; second compaction chains from previous `firstKeptEntryId`; role typing incl. tool-call args | `tests/test_case_and_store.py::test_tail_segmentation_by_first_kept_entry_id`, `::test_second_compaction_chains_from_previous_first_kept`, `::test_roles_typed_and_tool_call_args_flattened` |
| B1: dedupe by hash | Same summary in two sessions/stores audited once | `tests/test_case_and_store.py::test_census_dedupes_by_summary_hash`, `tests/test_parse.py::test_summary_hash_dedupe_key` |
| B1: identifier regex | Backticks >=4 chars, URLs, trailing punctuation stripped; present/absent fixture tokens | `tests/test_idcheck_retrieval.py::test_identifier_regex_backticks_and_urls`, `::test_idcheck_present_and_absent` |
| B1: cost guard | Stage-1 pre-run estimate (retrieval requests only) aborts before any call; stage-2 pre-check (spend so far + all built full-context requests) aborts before any full-context call and is skipped by `--no-fullctx`; mid-run abort when the next call could exceed; running total from reported usage | `tests/test_cost.py` (all tests), `tests/test_pipeline.py::test_midrun_abort_raises_cost_abort`, `::test_stage2_estimate_abort_after_retrieval`, `::test_no_fullctx_skips_stage2_precheck` |
| B1: no-write guard | Write under protected root refused; symlink from outside into the root refused; default `~/.openclaw` root checked without creating anything; `$OPENCLAW_STATE_DIR` honored; read-of-live-db refused | `tests/test_guard.py` (all tests) |
| B1: serial zstd decode | Decode raises off the main thread; works on it; mixed zstd/plain rows round-trip | `tests/test_case_and_store.py::test_decode_refuses_non_main_thread`, `::test_zstd_rows_round_trip` |
| B1: label-schema validation | Strict label/kind/index/conf validation; bad payloads rejected | `tests/test_judge.py::test_validate_items_*`, `::test_parse_judge_text_*` |
| B1: retry/backoff | ThrottlingException twice then success on a stubbed client (sleep patched); exhaustion raises; schema-invalid output retried then counted, not dropped | `tests/test_judge.py::test_retry_backoff_throttle_then_success`, `::test_retry_exhaustion_raises_judge_error`, `::test_schema_invalid_*` |
| B2: privacy | End-to-end audit with stub judge: no fixture entry string, no rationale in `report.md`/`report.json`; session ids hashed; `--include-text` file exists only in `--out`; report files written mode 0600 inside the 0700 out dir | `tests/test_cli.py::test_privacy_default_report_has_no_entry_text_or_rationales`, `::test_include_text_writes_private_file_only_in_out`, `::test_audit_passes_with_generous_budget` |
| B3: banned words | Prompts, labels, rendered reports, CLI output and all tracked files are free of the banned stems, matched case-insensitively (two stems plus one whole word, built from fragments in the test) | `tests/test_banned_words.py` (all tests) |
| B4: offline runs | `census` succeeds on fixtures (malformed DB skipped); `audit --judge stub --max-usd 0.01` aborts cleanly (exit != 0, clear message, no report files); generous budget passes end-to-end | `tests/test_cli.py::test_census_runs_offline`, `::test_audit_cost_abort_is_clean`, `::test_audit_passes_with_generous_budget` |
| B6: repo hygiene | No 12-digit ids, AKIA/ASIA keys, secret-style values, private-network addresses, UUIDs, absolute home-dir or protected-root paths, banned words in tracked content; no binary DBs committed | `tests/test_repo_hygiene.py` (all tests) |
| Pipeline behavior | Full-context stage promotes retrieval flags; `--no-fullctx` keeps retrieval labels; judge errors counted; cleanup deletes only marked snapshots; snapshot leaves no partial on malformed source; memory-mode git restore + guaranteed temp cleanup (also on error) | `tests/test_pipeline.py`, `tests/test_cli.py::test_audit_cleanup_deletes_only_marked_snapshots`, `::test_snapshot_malformed_source_leaves_no_partial` |

B5 (ruff, mypy, pytest clean; README content) is verified by running the gates;
results are recorded in the build report.

## Only provable with real data (post-handback, A1–A3)

| Criterion | Why it cannot be proven offline |
|---|---|
| A1: on the 49 real compactions, v2 non-sourced 13.3% ± 2 pp, confirmed 7.4% ± 2 pp, identifier rate 99.3% ± 0.3 | Requires the real snapshot corpus and live Sonnet judging. The stub judge is deterministic and synthetic; its label distribution proves plumbing, not judge behavior. Real-LLM behavior is not benchmarked here. |
| A2: full-corpus cost ≤ $15 and clean `--max-usd` abort on real data | Cost depends on real span sizes and judge verbosity. The abort mechanics are proven offline; the dollar figure is not. |
| A3: human precision check of 50 confirmed flags (95% CI; wording downgrade below 70%) | Needs private human review of real entry text via `--include-text`. |

## Known limitations

- The stub judge exercises the pipeline but not judge quality; no claim about
  real-LLM labeling accuracy is made (real-LLM behavior: not benchmarked).
- Retrieval false negatives are structurally unmeasured (full-context re-checks
  flagged entries only).
- `SOURCED:USER` principal sub-tag is always `unknown` in v0.1.
- Legacy free-form summaries are counted, never parsed or judged.
- Memory mode measures self-consistency between two agent-written artifacts,
  not ground truth.
- Mid-run abort cancels pending judge calls; an in-flight Bedrock call may
  still complete (and cost its tokens) before the abort surfaces.
