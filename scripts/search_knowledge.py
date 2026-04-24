#!/usr/bin/env python3
"""
search_knowledge.py — Hermes-Agent Knowledge Base Search CLI

Search past knowledge by keyword, category, tag, and environment fingerprint.
Default backend is JSONL; switches automatically to 'cli' when
HERMES_KB_CLI_SEARCH is set in the environment, or to 'http' when
HERMES_KB_BACKEND=http is set.
Runs on stdlib only. Upgrades to semantic search automatically when
sentence-transformers is installed.

Usage:
  python search_knowledge.py --query "CUDA OOM NCCL" --category error_resolution --limit 5
  python search_knowledge.py --list-index --format markdown
  python search_knowledge.py --id kb_20260423_143012_a1b2

See references/schema.md for the full schema and backend specs.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shlex
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

# ---------------------------------------------------------------------------
# Constants & configuration
# ---------------------------------------------------------------------------

def _resolve_kb_dir() -> Path:
    """Resolve the knowledge base directory.

    Priority:
      1. HERMES_KB_DIR  — explicit override
      2. WIKI_PATH      — LLM-Wiki Obsidian vault → Hermes-KB/ subfolder
      3. WIKI           — alias for WIKI_PATH
      4. fallback       — ~/.hermes/knowledge-base/
    """
    explicit = os.environ.get("HERMES_KB_DIR", "").strip()
    if explicit:
        return Path(explicit)
    for var in ("WIKI_PATH", "WIKI"):
        val = os.environ.get(var, "").strip()
        if val:
            return Path(val) / "Hermes-KB"
    return Path.home() / ".hermes" / "knowledge-base"


DEFAULT_KB_DIR = _resolve_kb_dir()

# Auto-select 'cli' when LLM-Wiki env vars are present; otherwise fall back to 'jsonl'
DEFAULT_BACKEND = os.environ.get(
    "HERMES_KB_BACKEND",
    "cli" if os.environ.get("HERMES_KB_CLI_SEARCH") else "jsonl"
).lower()

VALID_CATEGORIES = {
    "error_resolution",
    "environment_setup",
    "model_config",
    "workflow",
    "best_practice",
}

# Minimal English + Japanese stopwords for TF-IDF
STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "to", "of", "in",
    "on", "at", "for", "with", "by", "from", "as", "that", "this", "it", "and",
    "or", "but", "not", "no", "do", "does", "did", "have", "has", "had",
    "です", "ます", "する", "した", "ある", "いる", "これ", "それ", "あの", "この",
    "の", "に", "は", "を", "が", "と", "で", "も", "から", "まで", "へ",
}

# ---------------------------------------------------------------------------
# Tokenization (lightweight, ja+en)
# ---------------------------------------------------------------------------

TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_\-]*|\d+|[ぁ-んァ-ヶー一-龯]+")


def tokenize(text: str) -> list[str]:
    """Split text into lowercase tokens (English words, numbers, Japanese runs)."""
    if not text:
        return []
    tokens = TOKEN_RE.findall(text)
    out: list[str] = []
    for t in tokens:
        t_low = t.lower()
        if t_low in STOPWORDS:
            continue
        if len(t_low) < 2:
            continue
        out.append(t_low)
    return out


# ---------------------------------------------------------------------------
# Record helpers
# ---------------------------------------------------------------------------


def record_text_bag(record: dict) -> list[str]:
    """Flatten a record into a bag of tokens for TF-IDF scoring."""
    parts: list[str] = []
    for key in ("title", "summary"):
        parts.extend(tokenize(record.get(key, "")))
    err = record.get("error_symptoms") or {}
    parts.extend(tokenize(err.get("error_type", "")))
    parts.extend(tokenize(err.get("error_message", "")))
    parts.extend(tokenize(err.get("stack_trace_excerpt", "")))
    for step in record.get("solution_steps", []) or []:
        parts.extend(tokenize(step.get("action", "")))
        parts.extend(tokenize(step.get("rationale", "")))
    env = record.get("environment") or {}
    for v in env.values():
        if isinstance(v, str):
            parts.extend(tokenize(v))
        elif isinstance(v, dict):
            for vv in v.values():
                if isinstance(vv, str):
                    parts.extend(tokenize(vv))
    mc = record.get("model_config") or {}
    for v in mc.values():
        if isinstance(v, str):
            parts.extend(tokenize(v))
    # Tags are already lowercase snake_case
    parts.extend(record.get("tags", []) or [])
    return parts


# ---------------------------------------------------------------------------
# JSONL backend
# ---------------------------------------------------------------------------


def _kb_file() -> Path:
    return DEFAULT_KB_DIR / "kb.jsonl"


def _iter_records_jsonl(path: Path) -> Iterator[dict]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as e:
                print(f"[warn] skipping malformed line {line_no}: {e}", file=sys.stderr)


def _load_all_jsonl() -> list[dict]:
    return list(_iter_records_jsonl(_kb_file()))


# ---------------------------------------------------------------------------
# TF-IDF ranking
# ---------------------------------------------------------------------------


def _tfidf_scores(
    query_tokens: list[str], records: list[dict]
) -> list[tuple[float, dict]]:
    if not records:
        return []
    # Document frequency
    df: Counter[str] = Counter()
    bags: list[list[str]] = []
    for rec in records:
        bag = record_text_bag(rec)
        bags.append(bag)
        df.update(set(bag))
    n_docs = len(records)
    idf = {term: math.log((n_docs + 1) / (freq + 1)) + 1.0 for term, freq in df.items()}

    q_set = set(query_tokens)
    scored: list[tuple[float, dict]] = []
    for rec, bag in zip(records, bags):
        if not bag:
            continue
        tf = Counter(bag)
        score = 0.0
        overlap = 0
        for q in query_tokens:
            if q in tf:
                overlap += 1
                score += (tf[q] / len(bag)) * idf.get(q, 1.0)
        # Boost for title / tag overlap
        title_tokens = set(tokenize(rec.get("title", "")))
        if title_tokens & q_set:
            score *= 1.5
        if set(rec.get("tags", []) or []) & q_set:
            score *= 1.3
        # Recency decay (soft): ~37% weight reduction at 1 year
        try:
            ts = rec.get("updated_at") or rec.get("created_at")
            if ts:
                dt = datetime.fromisoformat(ts)
                age_days = (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).days
                decay = math.exp(-age_days / 365.0)
                score *= 0.5 + 0.5 * decay
        except Exception:
            pass
        # Confidence weighting
        conf = float(rec.get("confidence", 0.5))
        score *= 0.5 + 0.5 * conf
        if overlap > 0:
            scored.append((score, rec))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored


# ---------------------------------------------------------------------------
# Optional semantic reranking (sentence-transformers when available)
# ---------------------------------------------------------------------------


def _try_semantic_rerank(
    query: str, ranked: list[tuple[float, dict]], top_n: int = 20
) -> list[tuple[float, dict]]:
    """Rerank the top_n results using embedding similarity when sentence-transformers
    is installed. Falls back silently on ImportError or any runtime error."""
    try:
        from sentence_transformers import SentenceTransformer, util  # type: ignore
    except Exception:
        return ranked
    try:
        model_name = os.environ.get(
            "HERMES_KB_EMBED_MODEL",
            "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        )
        model = SentenceTransformer(model_name)
        head = ranked[:top_n]
        tail = ranked[top_n:]
        texts = [(r.get("title", "") + "\n" + r.get("summary", "")) for _, r in head]
        if not texts:
            return ranked
        q_emb = model.encode([query], convert_to_tensor=True, normalize_embeddings=True)
        d_emb = model.encode(texts, convert_to_tensor=True, normalize_embeddings=True)
        sims = util.cos_sim(q_emb, d_emb)[0].tolist()
        # Blend: 0.6 × TF-IDF (normalized) + 0.4 × semantic similarity
        max_tfidf = max((s for s, _ in head), default=1.0) or 1.0
        blended = []
        for (tfidf_s, rec), sim in zip(head, sims):
            blended_score = 0.6 * (tfidf_s / max_tfidf) + 0.4 * float(sim)
            blended.append((blended_score, rec))
        blended.sort(key=lambda x: x[0], reverse=True)
        return blended + tail
    except Exception as e:
        print(f"[info] semantic rerank skipped: {e}", file=sys.stderr)
        return ranked


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


def search_jsonl(args) -> list[dict]:
    records = _load_all_jsonl()
    # Structured filters
    if args.category:
        records = [r for r in records if r.get("category") == args.category]
    if args.tags:
        wanted = set(args.tags)
        records = [r for r in records if wanted & set(r.get("tags", []) or [])]
    if args.since:
        since = datetime.fromisoformat(args.since).astimezone(timezone.utc)

        def _ok(r):
            ts = r.get("updated_at") or r.get("created_at")
            if not ts:
                return False
            try:
                return datetime.fromisoformat(ts).astimezone(timezone.utc) >= since
            except Exception:
                return False

        records = [r for r in records if _ok(r)]
    # Query ranking
    if args.query:
        q_tokens = tokenize(args.query)
        ranked = _tfidf_scores(q_tokens, records)
        if not args.no_semantic:
            ranked = _try_semantic_rerank(args.query, ranked)
        hits = [rec for _, rec in ranked[: args.limit]]
    else:
        # No query: return most recently updated
        records.sort(key=lambda r: r.get("updated_at") or r.get("created_at") or "", reverse=True)
        hits = records[: args.limit]
    return hits


def search_cli(args) -> list[dict]:
    tmpl = os.environ.get("HERMES_KB_CLI_SEARCH")
    if not tmpl:
        raise RuntimeError("HERMES_KB_CLI_SEARCH not set for backend=cli")
    cmd_str = tmpl.format(
        query=shlex.quote(args.query or ""),
        category=shlex.quote(args.category or ""),
        limit=str(args.limit),
    )
    proc = subprocess.run(
        cmd_str, shell=True, capture_output=True, text=True, timeout=30
    )
    if proc.returncode != 0:
        raise RuntimeError(f"cli backend failed: rc={proc.returncode} stderr={proc.stderr[:500]}")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"cli backend returned non-JSON: {e}")
    if not isinstance(data, list):
        raise RuntimeError("cli backend must return a JSON array")
    return data[: args.limit]


def search_http(args) -> list[dict]:
    import urllib.request
    import urllib.error

    endpoint = os.environ.get("HERMES_KB_HTTP_ENDPOINT")
    if not endpoint:
        raise RuntimeError("HERMES_KB_HTTP_ENDPOINT not set for backend=http")
    timeout = float(os.environ.get("HERMES_KB_HTTP_TIMEOUT", "10"))
    body = json.dumps(
        {
            "query": args.query or "",
            "category": args.category,
            "tags": args.tags or [],
            "limit": args.limit,
            "since": args.since,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        endpoint.rstrip("/") + "/search",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    token = os.environ.get("HERMES_KB_HTTP_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        raise RuntimeError(f"http backend failed: {e}")
    if not isinstance(data, list):
        raise RuntimeError("http backend must return a JSON array")
    return data[: args.limit]


def dispatch_search(args) -> list[dict]:
    backend = (args.backend or DEFAULT_BACKEND).lower()
    try:
        if backend == "jsonl":
            return search_jsonl(args)
        if backend == "cli":
            return search_cli(args)
        if backend == "http":
            return search_http(args)
        raise RuntimeError(f"unknown backend: {backend}")
    except Exception as e:
        if args.no_fallback or backend == "jsonl":
            raise
        print(f"[warn] backend={backend} failed ({e}); falling back to jsonl", file=sys.stderr)
        return search_jsonl(args)


# ---------------------------------------------------------------------------
# Index listing
# ---------------------------------------------------------------------------


def build_index_markdown(records: list[dict]) -> str:
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_cat[r.get("category", "uncategorized")].append(r)
    now_jst = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    lines = [
        "# Hermes-Agent Knowledge Base Index",
        "",
        f"_Generated: {now_jst} / Total records: {len(records)}_",
        "",
    ]
    order = ["error_resolution", "environment_setup", "model_config", "workflow", "best_practice"]
    seen_cats = set()
    for cat in order + sorted(k for k in by_cat if k not in order):
        if cat not in by_cat or cat in seen_cats:
            continue
        seen_cats.add(cat)
        recs = sorted(
            by_cat[cat],
            key=lambda r: r.get("updated_at") or r.get("created_at") or "",
            reverse=True,
        )
        lines.append(f"## {cat} ({len(recs)})")
        lines.append("")
        for r in recs:
            tags = " ".join(f"`{t}`" for t in (r.get("tags") or [])[:5])
            lines.append(
                f"- `{r.get('id','?')}` **{r.get('title','(untitled)')}** — {tags} — "
                f"{r.get('outcome','?')} ({r.get('confidence','?')})"
            )
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def format_result_markdown(records: list[dict]) -> str:
    if not records:
        return "_No matching knowledge found._"
    blocks = [f"# Knowledge Search Results ({len(records)} hit{'s' if len(records) != 1 else ''})", ""]
    for i, r in enumerate(records, 1):
        blocks.append(f"## {i}. {r.get('title', '(untitled)')}  [`{r.get('id', '?')}`]")
        blocks.append("")
        blocks.append(f"- **category**: {r.get('category', '?')}")
        blocks.append(f"- **outcome**: {r.get('outcome', '?')} (confidence {r.get('confidence', '?')})")
        tags = r.get("tags") or []
        if tags:
            blocks.append(f"- **tags**: {' '.join('`' + t + '`' for t in tags)}")
        if r.get("environment"):
            env = r["environment"]
            env_flat = ", ".join(f"{k}={v}" for k, v in env.items() if isinstance(v, (str, int)))
            if env_flat:
                blocks.append(f"- **env**: {env_flat}")
        blocks.append("")
        blocks.append(f"**Summary:** {r.get('summary', '')}")
        blocks.append("")
        steps = r.get("solution_steps") or []
        if steps:
            blocks.append("**Solution steps:**")
            for s in steps:
                blocks.append(
                    f"  {s.get('order', '?')}. `{s.get('action', '')}` — {s.get('rationale', '')}"
                )
            blocks.append("")
    return "\n".join(blocks)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Search the Hermes-Agent knowledge base.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--query", "-q", help="Free-text query (error message, keywords, etc.)")
    p.add_argument("--category", choices=sorted(VALID_CATEGORIES), help="Filter by category")
    p.add_argument("--tags", nargs="*", default=None, help="Filter by tags (OR match)")
    p.add_argument("--since", help="ISO date (YYYY-MM-DD); only records updated after this date")
    p.add_argument("--limit", "-n", type=int, default=5, help="Max results (default 5)")
    p.add_argument("--id", dest="record_id", help="Fetch a specific record by ID")
    p.add_argument("--list-index", action="store_true", help="Print full KB index")
    p.add_argument("--format", choices=("json", "markdown"), default="json")
    p.add_argument("--backend", choices=("jsonl", "cli", "http"), help="Override HERMES_KB_BACKEND")
    p.add_argument("--no-fallback", action="store_true", help="Disable jsonl fallback on error")
    p.add_argument("--no-semantic", action="store_true", help="Disable sentence-transformers reranking")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # Normalise --tags: accept both "cuda,oom" (one arg) and cuda oom (two args)
    if args.tags:
        expanded: list[str] = []
        for token in args.tags:
            expanded.extend(t.strip() for t in token.split(",") if t.strip())
        args.tags = expanded

    # ID lookup shortcut (jsonl only; CLI/HTTP backends may extend)
    if args.record_id:
        for r in _load_all_jsonl():
            if r.get("id") == args.record_id:
                print(json.dumps(r, ensure_ascii=False, indent=2))
                return 0
        print(f"[error] id not found: {args.record_id}", file=sys.stderr)
        return 2

    # Index listing
    if args.list_index:
        records = _load_all_jsonl()
        if args.format == "markdown":
            print(build_index_markdown(records))
        else:
            print(
                json.dumps(
                    [
                        {
                            "id": r.get("id"),
                            "title": r.get("title"),
                            "category": r.get("category"),
                            "tags": r.get("tags", []),
                            "outcome": r.get("outcome"),
                            "confidence": r.get("confidence"),
                        }
                        for r in records
                    ],
                    ensure_ascii=False,
                    indent=2,
                )
            )
        return 0

    # Normal search
    hits = dispatch_search(args)
    if args.format == "markdown":
        print(format_result_markdown(hits))
    else:
        print(json.dumps(hits, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
