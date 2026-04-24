#!/usr/bin/env python3
"""
update_knowledge.py — Hermes-Agent Knowledge Base Update CLI

Save a new knowledge record to the persistent store.
Validates input JSON against the schema, warns about near-duplicates,
and regenerates the index after every successful write.
Backend: cli (LLM-Wiki, auto-selected when HERMES_KB_CLI_SEARCH is set),
         jsonl (local fallback), or http.
Runs on stdlib only.

IMPORTANT: This script is the ONLY permitted way to write to the knowledge base.
Do not use write_file, patch, edit, or execute_code to modify kb.jsonl or index.md.
index.md is automatically regenerated here — any direct edit will be overwritten.

Usage:
  # From a file
  python update_knowledge.py --input-file new.json

  # From stdin
  cat new.json | python update_knowledge.py --stdin

  # Merge into an existing record
  python update_knowledge.py --input-file update.json --merge-with kb_20260423_143012_a1b2

See references/schema.md for the full schema.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _resolve_kb_dir() -> Path:
    """Resolve the knowledge base directory.

    Priority:
      1. HERMES_KB_DIR  — explicit override, use as-is
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
VALID_OUTCOMES = {"success", "failure", "partial"}
JST = timezone(timedelta(hours=9))

# ---------------------------------------------------------------------------
# Schema validation (dependency-free)
# ---------------------------------------------------------------------------


class ValidationError(Exception):
    pass


def _req(obj: dict, key: str, type_, path: str) -> Any:
    if key not in obj:
        raise ValidationError(f"missing required field: {path}.{key}")
    v = obj[key]
    if type_ is float:
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            raise ValidationError(f"{path}.{key} must be number, got {type(v).__name__}")
    elif not isinstance(v, type_):
        raise ValidationError(
            f"{path}.{key} must be {type_.__name__}, got {type(v).__name__}"
        )
    return v


def validate_record(rec: dict) -> None:
    """Validate a record against the schema. Raises ValidationError on failure."""
    if not isinstance(rec, dict):
        raise ValidationError(f"record must be an object, got {type(rec).__name__}")

    cat = _req(rec, "category", str, "$")
    if cat not in VALID_CATEGORIES:
        raise ValidationError(
            f"$.category must be one of {sorted(VALID_CATEGORIES)}, got '{cat}'"
        )

    title = _req(rec, "title", str, "$")
    if not (8 <= len(title) <= 60):
        raise ValidationError(
            f"$.title length must be 8–60 chars, got {len(title)}"
        )

    summary = _req(rec, "summary", str, "$")
    if not (20 <= len(summary) <= 800):
        raise ValidationError(
            f"$.summary length must be 20–800 chars, got {len(summary)}"
        )

    outcome = _req(rec, "outcome", str, "$")
    if outcome not in VALID_OUTCOMES:
        raise ValidationError(f"$.outcome must be one of {VALID_OUTCOMES}, got '{outcome}'")

    conf = _req(rec, "confidence", float, "$")
    if not (0.0 <= conf <= 1.0):
        raise ValidationError(f"$.confidence must be in [0, 1], got {conf}")

    # solution_steps is optional when outcome == "failure" (nothing yet worked)
    if outcome != "failure" or "solution_steps" in rec:
        steps = _req(rec, "solution_steps", list, "$")
        if len(steps) < 1:
            raise ValidationError("$.solution_steps must have at least 1 item")
    else:
        steps = []
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            raise ValidationError(f"$.solution_steps[{i}] must be object")
        _req(step, "order", int, f"$.solution_steps[{i}]")
        if not isinstance(step.get("action"), str) or not step["action"].strip():
            raise ValidationError(f"$.solution_steps[{i}].action must be non-empty string")
        if not isinstance(step.get("rationale"), str) or not step["rationale"].strip():
            raise ValidationError(f"$.solution_steps[{i}].rationale must be non-empty string")

    # error_symptoms required when category == error_resolution
    if cat == "error_resolution":
        if "error_symptoms" not in rec or not isinstance(rec["error_symptoms"], dict):
            raise ValidationError(
                "$.error_symptoms required (object) when category=error_resolution"
            )
        es = rec["error_symptoms"]
        if not isinstance(es.get("error_type"), str) or not es["error_type"].strip():
            raise ValidationError("$.error_symptoms.error_type required")
        if not isinstance(es.get("error_message"), str) or not es["error_message"].strip():
            raise ValidationError("$.error_symptoms.error_message required")

    # Tag format validation
    for i, t in enumerate(rec.get("tags") or []):
        if not isinstance(t, str) or not re.match(r"^[a-z0-9_]+$", t):
            raise ValidationError(
                f"$.tags[{i}] must match /^[a-z0-9_]+$/, got '{t}'"
            )

    # Optional object type checks
    for opt_obj in ("environment", "model_config", "source"):
        if opt_obj in rec and not isinstance(rec[opt_obj], dict):
            raise ValidationError(f"$.{opt_obj} must be object")
    if "related_ids" in rec and not isinstance(rec["related_ids"], list):
        raise ValidationError("$.related_ids must be array")


# ---------------------------------------------------------------------------
# ID & timestamps
# ---------------------------------------------------------------------------


def generate_id(rec: dict) -> str:
    now = datetime.now(JST)
    stamp = now.strftime("%Y%m%d_%H%M%S")
    # 4-char hash of title+summary to disambiguate records generated in the same second
    h = hashlib.sha1(
        (rec.get("title", "") + rec.get("summary", "")).encode("utf-8")
    ).hexdigest()[:4]
    return f"kb_{stamp}_{h}"


def now_iso() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# JSONL backend — atomic append
# ---------------------------------------------------------------------------


def _kb_file() -> Path:
    DEFAULT_KB_DIR.mkdir(parents=True, exist_ok=True)
    return DEFAULT_KB_DIR / "kb.jsonl"


def _index_file() -> Path:
    return DEFAULT_KB_DIR / "index.md"


def _records_dir() -> Path:
    """Directory for individual Obsidian .md files (one per knowledge record)."""
    d = DEFAULT_KB_DIR / "records"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _record_filename(record: dict) -> str:
    """Generate a readable filename: {sanitized_title}_{short_id}.md"""
    title = record.get("title", "untitled")
    safe = re.sub(r"[^\w\s\-]", "", title)
    safe = re.sub(r"\s+", "_", safe.strip())[:48]
    short_id = (record.get("id") or "xxxx")[-4:]
    return f"{safe}_{short_id}.md"


def record_to_wiki_md(record: dict) -> str:
    """Render a knowledge record as an Obsidian-compatible Markdown note."""
    tags = record.get("tags") or []
    related = record.get("related_ids") or []

    # YAML frontmatter (no external dependency)
    fm: list[str] = [
        "---",
        f"id: {record.get('id', '')}",
        f"created: \"{record.get('created_at', '')}\"",
        f"updated: \"{record.get('updated_at', '')}\"",
        f"category: {record.get('category', '')}",
        f"outcome: {record.get('outcome', '')}",
        f"confidence: {record.get('confidence', '')}",
        "tags:" + (" []" if not tags else ""),
    ]
    for t in tags:
        fm.append(f"  - {t}")
    fm.append("related_ids:" + (" []" if not related else ""))
    for r in related:
        fm.append(f"  - {r}")
    fm.append("---")

    body: list[str] = ["\n".join(fm), "", f"# {record.get('title', 'Untitled')}", ""]

    if record.get("summary"):
        body += [f"> {record['summary']}", ""]

    env = record.get("environment") or {}
    if env:
        body += ["## Environment", "", "| Field | Value |", "|-------|-------|"]
        for k, v in env.items():
            if isinstance(v, dict):
                for kk, vv in v.items():
                    body.append(f"| `{k}.{kk}` | {vv} |")
            elif v is not None:
                body.append(f"| `{k}` | {v} |")
        body.append("")

    mc = record.get("model_config") or {}
    if mc:
        body += ["## Model Config", "", "| Field | Value |", "|-------|-------|"]
        for k, v in mc.items():
            if isinstance(v, dict):
                for kk, vv in v.items():
                    body.append(f"| `{k}.{kk}` | {vv} |")
            elif v is not None:
                body.append(f"| `{k}` | {v} |")
        body.append("")

    es = record.get("error_symptoms") or {}
    if es:
        body += ["## Error Symptoms", ""]
        if es.get("error_type"):
            body.append(f"- **Type:** `{es['error_type']}`")
        if es.get("error_message"):
            body.append(f"- **Message:** `{es['error_message']}`")
        if es.get("frequency"):
            body.append(f"- **Frequency:** {es['frequency']}")
        if es.get("reproduction"):
            body += ["", "**Reproduction:**"]
            for step in es["reproduction"]:
                body.append(f"- {step}")
        if es.get("stack_trace_excerpt"):
            body += ["", "**Stack trace (excerpt):**", "```", es["stack_trace_excerpt"], "```"]
        body.append("")

    steps = record.get("solution_steps") or []
    if steps:
        body += ["## Solution Steps", ""]
        for s in steps:
            body.append(f"{s.get('order', '?')}. **`{s.get('action', '')}`**  ")
            body.append(f"   *{s.get('rationale', '')}*  ")
            if s.get("verification"):
                body.append(f"   ✔ {s['verification']}  ")
            body.append("")

    src = record.get("source") or {}
    meta_lines: list[str] = []
    if src.get("conversation_id"):
        meta_lines.append(f"- **Session:** `{src['conversation_id']}`")
    if src.get("agent_version"):
        meta_lines.append(f"- **Agent:** {src['agent_version']}")
    if src.get("user_hint"):
        meta_lines.append(f"- **Note:** {src['user_hint']}")
    if meta_lines:
        body += ["## Source", ""] + meta_lines + [""]

    return "\n".join(body)


def write_wiki_md(record: dict) -> Path:
    """Write/overwrite the record's .md file in the records directory."""
    rdir = _records_dir()
    fname = _record_filename(record)
    out_path = rdir / fname
    out_path.write_text(record_to_wiki_md(record), encoding="utf-8")
    return out_path


def delete_wiki_md(record: dict) -> None:
    """Remove the .md file for a record (called before re-writing on merge)."""
    p = _records_dir() / _record_filename(record)
    if p.exists():
        p.unlink()


def _load_all() -> list[dict]:
    p = _kb_file()
    if not p.exists():
        return []
    out: list[dict] = []
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _write_all_atomic(records: list[dict]) -> None:
    p = _kb_file()
    tmp = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", delete=False, dir=str(p.parent), suffix=".tmp"
    )
    try:
        for r in records:
            tmp.write(json.dumps(r, ensure_ascii=False) + "\n")
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp.name, p)
    except Exception:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass
        raise


def _append_atomic(record: dict) -> None:
    """Append a single record durably. Uses O_APPEND for atomic line append on POSIX."""
    p = _kb_file()
    line = json.dumps(record, ensure_ascii=False) + "\n"
    # POSIX: O_APPEND is atomic for writes <= PIPE_BUF (~512 bytes).
    # For larger records, fall back to full rewrite.
    encoded = line.encode("utf-8")
    if len(encoded) <= 512:
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, encoded)
            os.fsync(fd)
        finally:
            os.close(fd)
    else:
        all_recs = _load_all()
        all_recs.append(record)
        _write_all_atomic(all_recs)


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def detect_duplicates(new_rec: dict, existing: list[dict], threshold: float = 0.85) -> list[dict]:
    """Return existing records with title+summary closely resembling new_rec."""
    target = (new_rec.get("title", "") + "\n" + new_rec.get("summary", "")).lower()
    candidates = []
    for r in existing:
        # Cross-category near-matches are not considered duplicates
        if r.get("category") != new_rec.get("category"):
            continue
        other = (r.get("title", "") + "\n" + r.get("summary", "")).lower()
        sim = _similarity(target, other)
        tag_overlap = len(set(new_rec.get("tags") or []) & set(r.get("tags") or []))
        if sim >= threshold or (sim >= 0.7 and tag_overlap >= 2):
            candidates.append((sim, r))
    candidates.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in candidates]


def merge_records(old: dict, new: dict) -> dict:
    """Merge new into old; new scalar fields take precedence, lists are unioned."""
    merged = dict(old)
    for key in (
        "title", "summary", "category", "outcome", "confidence",
        "environment", "model_config", "error_symptoms",
    ):
        if key in new and new[key] is not None:
            merged[key] = new[key]
    # solution_steps: replace entirely when new provides it
    if new.get("solution_steps"):
        merged["solution_steps"] = new["solution_steps"]
    # Union-style lists
    merged["tags"] = sorted(set((merged.get("tags") or []) + (new.get("tags") or [])))
    merged["related_ids"] = sorted(
        set((merged.get("related_ids") or []) + (new.get("related_ids") or []))
    )
    if "source" in new:
        merged["source"] = {**(merged.get("source") or {}), **new["source"]}
    merged["updated_at"] = now_iso()
    # Preserve original id and created_at
    merged["id"] = old["id"]
    merged["created_at"] = old.get("created_at", now_iso())
    return merged


# ---------------------------------------------------------------------------
# Index regeneration
# ---------------------------------------------------------------------------


def regenerate_index(records: list[dict]) -> None:
    """Rebuild index.md from the current record set. Called automatically after every write."""
    from collections import defaultdict

    by_cat: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_cat[r.get("category", "uncategorized")].append(r)

    now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    lines = [
        "# Hermes-Agent Knowledge Base Index",
        "",
        f"_Generated: {now_str} / Total records: {len(records)}_",
        "",
    ]
    order = ["error_resolution", "environment_setup", "model_config", "workflow", "best_practice"]
    seen = set()
    for cat in order + sorted(k for k in by_cat if k not in order):
        if cat not in by_cat or cat in seen:
            continue
        seen.add(cat)
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
                f"- `{r.get('id', '?')}` **{r.get('title', '(untitled)')}** — {tags} — "
                f"{r.get('outcome', '?')} ({r.get('confidence', '?')})"
            )
        lines.append("")
    _index_file().write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


def upsert_jsonl(record: dict, args) -> dict:
    existing = _load_all()

    # Merge mode
    if args.merge_with:
        found = next((r for r in existing if r.get("id") == args.merge_with), None)
        if not found:
            raise RuntimeError(f"merge target id not found: {args.merge_with}")
        delete_wiki_md(found)          # remove old .md before overwriting
        merged = merge_records(found, record)
        new_list = [merged if r.get("id") == args.merge_with else r for r in existing]
        _write_all_atomic(new_list)
        regenerate_index(new_list)
        md_path = write_wiki_md(merged)
        print(f"[info] wiki note updated: {md_path}", file=sys.stderr)
        return merged

    # New record path
    if "id" not in record:
        record["id"] = generate_id(record)
    record.setdefault("created_at", now_iso())
    record["updated_at"] = now_iso()

    dups = detect_duplicates(record, existing)
    if dups and not args.force:
        raise RuntimeError(
            "possible duplicate(s) detected:\n"
            + "\n".join(f"  - {d['id']} :: {d.get('title', '')}" for d in dups[:5])
            + "\nUse --merge-with <id> to merge, or --force to insert anyway."
        )

    _append_atomic(record)
    regenerate_index(_load_all())
    md_path = write_wiki_md(record)
    print(f"[info] wiki note written: {md_path}", file=sys.stderr)
    return record


def upsert_cli(record: dict, args) -> dict:
    tmpl = os.environ.get("HERMES_KB_CLI_UPDATE")
    if not tmpl:
        raise RuntimeError("HERMES_KB_CLI_UPDATE not set for backend=cli")
    payload = json.dumps(record, ensure_ascii=False)
    proc = subprocess.run(
        tmpl, shell=True, input=payload, capture_output=True, text=True, timeout=30
    )
    if proc.returncode != 0:
        raise RuntimeError(f"cli backend failed: rc={proc.returncode} stderr={proc.stderr[:500]}")
    # Some CLIs echo the saved record; try to parse it
    out = proc.stdout.strip()
    if out:
        try:
            echoed = json.loads(out)
            if isinstance(echoed, dict):
                return echoed
        except json.JSONDecodeError:
            pass
    return record


def upsert_http(record: dict, args) -> dict:
    import urllib.request
    import urllib.error

    endpoint = os.environ.get("HERMES_KB_HTTP_ENDPOINT")
    if not endpoint:
        raise RuntimeError("HERMES_KB_HTTP_ENDPOINT not set for backend=http")
    timeout = float(os.environ.get("HERMES_KB_HTTP_TIMEOUT", "10"))
    body = json.dumps(record, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        endpoint.rstrip("/") + "/upsert",
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
    if isinstance(data, dict):
        return data
    return record


def dispatch_upsert(record: dict, args) -> dict:
    backend = (args.backend or DEFAULT_BACKEND).lower()
    try:
        if backend == "jsonl":
            return upsert_jsonl(record, args)
        if backend == "cli":
            return upsert_cli(record, args)
        if backend == "http":
            return upsert_http(record, args)
        raise RuntimeError(f"unknown backend: {backend}")
    except Exception as e:
        if args.no_fallback or backend == "jsonl":
            raise
        print(f"[warn] backend={backend} failed ({e}); falling back to jsonl", file=sys.stderr)
        return upsert_jsonl(record, args)


# ---------------------------------------------------------------------------
# Input loading
# ---------------------------------------------------------------------------


def load_input(args) -> dict:
    if args.input_file:
        raw = Path(args.input_file).read_text(encoding="utf-8")
    elif args.stdin:
        raw = sys.stdin.read()
    else:
        raise SystemExit("error: provide --input-file or --stdin")
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SystemExit(f"error: input is not valid JSON: {e}")
    if not isinstance(obj, dict):
        raise SystemExit("error: input must be a single JSON object (not array)")
    return obj


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Insert or update a knowledge record in the Hermes-Agent knowledge base.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    src = p.add_mutually_exclusive_group()
    src.add_argument("--input-file", "-f", help="Path to a JSON file containing a record")
    src.add_argument("--stdin", action="store_true", help="Read JSON record from stdin")
    p.add_argument("--merge-with", help="Merge into an existing record by ID")
    p.add_argument("--force", action="store_true", help="Insert even if duplicates are detected")
    p.add_argument("--dry-run", action="store_true", help="Validate only -- do not write")
    p.add_argument("--backend", choices=("jsonl", "cli", "http"), help="Override HERMES_KB_BACKEND")
    p.add_argument("--no-fallback", action="store_true", help="Disable jsonl fallback on error")
    p.add_argument("--quiet", "-q", action="store_true", help="Minimal output (id only)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    record = load_input(args)

    try:
        if not args.merge_with:
            validate_record(record)
        else:
            if "category" in record and record["category"] not in VALID_CATEGORIES:
                raise ValidationError(f"invalid category: {record['category']}")
    except ValidationError as e:
        print(f"[error] validation failed: {e}", file=sys.stderr)
        return 2

    if args.dry_run:
        print("[ok] validation passed (dry-run; not written)")
        return 0

    try:
        saved = dispatch_upsert(record, args)
    except Exception as e:
        print(f"[error] upsert failed: {e}", file=sys.stderr)
        return 3

    if args.quiet:
        print(saved.get("id", ""))
    else:
        print(json.dumps({"ok": True, "id": saved.get("id"), "record": saved}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
