# LLM News Ledger Template and Update Contract

This document defines the stable shared-memory format for `LLM Midday Brief` and `LLM Night Lite`.

## Files

| Path | Role |
|---|---|
| `state/llm-news-seen.jsonl` | Canonical machine-readable duplicate ledger. One JSON object per line. |
| `state/llm-news-ledger.md` | Human-readable summary and policy file. |
| `state/llm-news-ledger-template.md` | This schema and update contract. |

## Non-negotiable rules

1. Preserve JSONL validity.
2. Preserve all existing records unless the user explicitly requests cleanup.
3. Do not change field names.
4. Do not localize JSON keys.
5. Do not put Markdown fences inside `llm-news-seen.jsonl`.
6. Do not convert JSONL into a Markdown table.
7. Do not claim ledger-based duplicate checking succeeded when the ledger could not be fetched or parsed.
8. When reusing a known topic, require a concrete `前回からの差分:` sentence.
9. If no hard delta exists, classify the topic as `DUPLICATE` or `ONGOING_NO_NEWS`.
10. If writeback fails, still produce the news report but clearly state `LEDGER_WRITE_FAILED`.

## JSONL record types

### Meta record

Exactly one meta record should exist near the top.

```json
{
  "record_type": "meta",
  "schema_version": 1,
  "ledger_name": "LLM News Shared Ledger",
  "created_jst": "YYYY-MM-DDTHH:mm:ss+09:00",
  "updated_jst": "YYYY-MM-DDTHH:mm:ss+09:00",
  "repository": "4i7/knowledge-distiller",
  "ledger_md_path": "state/llm-news-ledger.md",
  "seen_jsonl_path": "state/llm-news-seen.jsonl",
  "template_path": "state/llm-news-ledger-template.md",
  "purpose": "Shared duplicate-control ledger for LLM Midday Brief and LLM Night Lite scheduled reports.",
  "seed_limitations": "..."
}
```

### Topic record

Use one topic record per canonical story. A story remains the same story even if a different media outlet, X thread, Reddit post, HN discussion, benchmark screenshot, or summary article discusses it.

```json
{
  "record_type": "topic",
  "schema_version": 1,
  "topic_key": "organization/product/event-type/YYYY-MM-DD",
  "canonical_title": "Short stable title",
  "aliases": ["alias 1", "alias 2"],
  "organizations": ["Organization"],
  "products": ["Product"],
  "event_type": "model_release|availability_restore|pricing|benchmark|tooling_model_integration|policy_corporate|safety_security_report|outage|rumor|other",
  "first_seen_jst": "YYYY-MM-DDTHH:mm:ss+09:00",
  "last_seen_jst": "YYYY-MM-DDTHH:mm:ss+09:00",
  "last_report": "LLM Midday Brief|LLM Night Lite",
  "coverage_status": "covered|covered_watch|watch_only|duplicate|superseded",
  "treatment": "How future runs should treat this story.",
  "known_facts": ["Fact already covered."],
  "reinclude_only_if": ["Hard-delta condition."],
  "sources": ["https://..."],
  "seed_basis": "scheduled_run|available_task_history_summary|manual_user_instruction",
  "confidence": 0.0,
  "notes": ""
}
```

## Topic-key rules

`topic_key` must be stable, lowercase where practical, and specific enough to avoid merging unrelated stories.

Recommended form:

```text
organization/product-or-model/event-type/YYYY-MM-DD
```

Examples:

```text
anthropic/fable-5/global-restore/2026-07-01
github/copilot/kimi-k2.7-code-browser-tools-credit-limits/2026-07-02
huggingface/metacognition-benchmark/2026-07-02
```

## Candidate classification rules

For each candidate topic:

| Classification | Meaning | Main report eligibility |
|---|---|---|
| `NEW` | No matching topic or alias exists in ledger. | Eligible |
| `FOLLOW_UP` | Matching known topic exists, but there is a hard delta. | Eligible only with `続報:` and `前回からの差分:` |
| `DUPLICATE` | Same topic, no hard delta. | Not eligible |
| `ONGOING_NO_NEWS` | Still discussed, but no new factual development. | Not eligible except compact watch log |
| `UNCONFIRMED_SIGNAL` | Rumor/community signal that may matter. | Eligible only if clearly labeled and high-signal |

## Hard delta definition

A meaningful delta includes at least one of:

- New official confirmation, blog post, release note, model card, pricing page, benchmark page, safety report, license, API availability, weights availability, deprecation notice, outage resolution, restriction, or policy change.
- New credible reporting that adds previously unknown factual information.
- New reproducible technical finding from a credible developer/researcher.
- Major adoption or breakage signal that changes practical interpretation.
- Significant correction to an earlier understanding.

Not a hard delta by itself:

- Another summary article repeating the same facts.
- Users still arguing on X, Reddit, HN, or Discord.
- Generic benchmark screenshots without methodology or reproducibility.
- Vague rumors, engagement bait, or memes.
- Search results surfacing the same story again because it is popular.

## Scheduled-task writeback protocol

At the end of each scheduled report:

1. Re-fetch `state/llm-news-seen.jsonl` immediately before writing, so the latest blob SHA is used.
2. Merge new or updated topic records.
3. Preserve every existing record and field unless intentionally updating:
   - `last_seen_jst`
   - `last_report`
   - `coverage_status`
   - `known_facts`
   - `reinclude_only_if`
   - `sources`
   - `notes`
4. Update the meta record's `updated_jst`.
5. Write the complete JSONL file back to GitHub.
6. Update `state/llm-news-ledger.md` only as a human-readable summary.
7. If there is a SHA conflict, re-fetch, merge again, and retry once.
8. If writeback still fails, state `LEDGER_WRITE_FAILED` in the final report.

## Report wording requirements

If a topic is repeated:

```text
続報: <topic>
前回からの差分: <specific factual delta>
```

If ledger access fails:

```text
検索・判定メモ: GitHub ledger could not be fetched or parsed; duplicate checking was limited to visible current-run context.
```

If ledger writeback fails:

```text
検索・判定メモ: LEDGER_WRITE_FAILED. The report was generated, but the shared GitHub ledger was not updated.
```

## Maintenance principle

This ledger is not a news archive. It is a deduplication and treatment-control layer.

Keep records short, stable, and operational.
