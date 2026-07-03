# LLM News Shared Ledger

Shared duplicate-control ledger for the scheduled `LLM Midday Brief` and `LLM Night Lite` tasks.

This file is the human-readable control surface. The canonical machine-readable state is `state/llm-news-seen.jsonl`. The update contract and record schema are in `state/llm-news-ledger-template.md`.

## Current status

| Field | Value |
|---|---|
| Schema version | 1 |
| Repository | `4i7/knowledge-distiller` |
| Ledger MD | `state/llm-news-ledger.md` |
| Seen JSONL | `state/llm-news-seen.jsonl` |
| Template | `state/llm-news-ledger-template.md` |
| Created | 2026-07-03 JST |
| Intended users | `LLM Midday Brief`, `LLM Night Lite` |

## Important limitation of the initial seed

The initial seed was created from available ChatGPT task/conversation history summaries, not from the complete bodies of all historical scheduled outputs. Therefore:

- Treat the seeded records as a conservative duplicate-prevention baseline.
- Do not claim that this file is a complete archive of all prior reports.
- Future scheduled runs must maintain this ledger directly so that later duplicate checks are based on explicit ledger state rather than fragile task-history visibility.

## Core rule

A topic that is already in `llm-news-seen.jsonl` must not be reintroduced as a main news item unless there is a concrete hard delta.

A valid repeat item must be labeled `続報:` and must include a short sentence beginning with:

> `前回からの差分:`

If that sentence cannot be written concretely, the item is `DUPLICATE` or `ONGOING_NO_NEWS`, not a main item.

## Initial covered topics

| Topic key | Canonical title | Last report | Treatment |
|---|---|---|---|
| `openai/gpt-5.6-sol-terra-luna/limited-preview/2026-06-27` | GPT-5.6 Sol/Terra/Luna limited preview | LLM Night Lite | Covered. Repeat only for access/pricing/API/docs/rollout/correction. |
| `anthropic/mythos-5/partial-restore/2026-06-27` | Anthropic Mythos 5 partial or limited restore | LLM Night Lite | Ongoing restore story. Repeat only for scope/API/eligibility/restriction deltas. |
| `anthropic/fable-5/global-restore/2026-07-01` | Anthropic Claude Fable 5 global restore | LLM Night Lite | High duplicate risk. Repeat only with a hard delta. |
| `github/copilot/kimi-k2.7-code-browser-tools-credit-limits/2026-07-02` | GitHub Copilot Kimi K2.7 Code, browser tools, and AI credit limits | LLM Midday Brief | Covered. Repeat only for rollout/pricing/credit/model/tool behavior changes. |
| `huggingface/metacognition-benchmark/2026-07-02` | Hugging Face metacognition benchmark | LLM Midday Brief | Covered. Repeat only for benchmark/leaderboard/methodology/adoption deltas. |
| `dukaanbench/benchmark/published/2026-06-27` | DukaanBench newly published | LLM Night Lite | Covered. Repeat only for benchmark revision, critique, or adoption. |
| `eu/austria-anthropic-attraction-policy/2026-06-28` | Austria urging EU to attract Anthropic | LLM Night Lite | Covered. Repeat only for policy action, Anthropic response, or new factual reporting. |
| `glm-5.2/cyber-capability-report/2026-06-28` | GLM-5.2 cyber-capability report | LLM Night Lite | Covered. Keep defensive/high-level. Repeat only for report/policy/evaluation deltas. |

## Required update behavior for scheduled tasks

Each scheduled LLM News task must do the following:

1. Fetch `state/llm-news-ledger-template.md`, `state/llm-news-ledger.md`, and `state/llm-news-seen.jsonl` from `4i7/knowledge-distiller`.
2. Parse every non-empty line in `llm-news-seen.jsonl` as exactly one JSON object.
3. Build a duplicate ledger from:
   - `topic_key`
   - `canonical_title`
   - `aliases`
   - `organizations`
   - `products`
   - `sources`
4. Classify every candidate as:
   - `NEW`
   - `FOLLOW_UP`
   - `DUPLICATE`
   - `ONGOING_NO_NEWS`
   - `UNCONFIRMED_SIGNAL`
5. Include only `NEW`, strong `FOLLOW_UP`, and high-signal labeled `UNCONFIRMED_SIGNAL` in the main report.
6. After writing the report, update `llm-news-seen.jsonl` and this MD file.
7. If GitHub ledger read/write fails, explicitly state the failure in `検索・判定メモ` and do not pretend that historical duplicate checking succeeded.

## File ownership

- `llm-news-seen.jsonl` is the canonical machine state.
- `llm-news-ledger.md` is a human-readable index and policy summary.
- `llm-news-ledger-template.md` is the schema and update contract.
- Do not let scheduled tasks invent a new schema.
- Do not let scheduled tasks replace JSONL with prose.
- Do not remove existing records unless the user explicitly requests cleanup.
