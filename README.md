# knowledge-distiller

A meta-skill for [Hermes-Agent](https://github.com/Leoleojames1/Hermes-Agent) that gives local LLMs persistent, searchable memory across sessions.

The goal is simple: **never solve the same problem twice, never look up the same environment twice.**

Each resolved error, verified environment, and effective configuration is extracted into a structured knowledge base (LLM-Wiki / Obsidian vault) and retrieved automatically at the start of future sessions.

---

## How it works

```
conversation  →  [LLM extracts structured JSON]  →  update_knowledge.py  →  kb.jsonl + .md
future session  →  search_knowledge.py  →  [LLM reads hits]  →  faster diagnosis
```

- **LLM role**: extract and judge (conversation → schema, search results → response)
- **Script role**: all storage I/O, deduplication, index rebuilding

---

## Requirements

- Python 3.9+ (stdlib only for core functionality)
- Hermes-Agent with bash tool access
- Optional: `sentence-transformers` for semantic reranking (`pip install sentence-transformers`)

---

## Installation

```bash
git clone https://github.com/4i7/knowledge-distiller
cd knowledge-distiller
bash install.sh
```

The skill installs to `~/.hermes/skills/knowledge-distiller` by default.

**Environment variables** (add to your shell profile):

```bash
# Required: point to your Obsidian/LLM-Wiki vault
export WIKI_PATH="/path/to/your/obsidian/vault"

# Optional overrides
export HERMES_KB_DIR="/custom/kb/path"          # explicit KB location
export KNOWLEDGE_DISTILLER_SKILL_DIR="..."      # if installed to a non-default path
export HERMES_MODEL_SIZE="small"                # small | medium | large (affects result limits)
```

---

## Storage layout

```
$WIKI_PATH/Hermes-KB/
├── kb.jsonl              ← JSONL index (used by search)
├── index.md              ← auto-generated human-readable index
└── records/
    ├── NCCL_timeout_bond0_a1b2.md    ← Obsidian-compatible notes
    └── ...
```

Records in `records/` appear as regular notes in Obsidian. **Do not edit them directly** — edits do not propagate back to `kb.jsonl`. Use `update_knowledge.py --merge-with <id>` instead.

---

## Quick reference

**Search:**
```bash
SKILL_DIR="${KNOWLEDGE_DISTILLER_SKILL_DIR:-$HOME/.hermes/skills/knowledge-distiller}"
python "$SKILL_DIR/scripts/search_knowledge.py" \
  --query "NCCL timeout DDP" \
  --category error_resolution \
  --tags nccl ddp \
  --limit 5 --format json
```

**Save:**
```bash
SKILL_DIR="${KNOWLEDGE_DISTILLER_SKILL_DIR:-$HOME/.hermes/skills/knowledge-distiller}"
cat <<'JSON' | python "$SKILL_DIR/scripts/update_knowledge.py" --stdin
{
  "category": "error_resolution",
  "title": "NCCL timeout on bond0 interface",
  "summary": "NCCL timed out on 2-node DDP because bond0 caused MTU mismatch. Setting NCCL_SOCKET_IFNAME=eth0 resolved it.",
  "environment": {"os": "Ubuntu 22.04", "cuda": "12.1", "gpu": "A100 x8"},
  "error_symptoms": {
    "error_type": "RuntimeError",
    "error_message": "NCCL timeout after 1800s",
    "frequency": "always"
  },
  "solution_steps": [
    {"order": 1, "action": "export NCCL_SOCKET_IFNAME=eth0", "rationale": "bond0 MTU mismatch causes timeout"},
    {"order": 2, "action": "relaunch torchrun", "rationale": "env var must be set before process start"}
  ],
  "outcome": "success",
  "confidence": 0.9,
  "tags": ["nccl", "ddp", "networking"]
}
JSON
```

**List all records:**
```bash
python "$SKILL_DIR/scripts/search_knowledge.py" --list-index --format markdown
```

---

## Record schema

See [`references/schema.md`](references/schema.md) for the full schema, JSON Schema definition, and canonical examples.

**Categories:** `error_resolution` / `environment_setup` / `model_config` / `workflow` / `best_practice`

**Key fields:**

| Field | Required | Notes |
|---|---|---|
| `category` | Yes | one of the five above |
| `title` | Yes | 8–60 chars |
| `summary` | Yes | 2–4 sentences, self-contained |
| `outcome` | Yes | `success` / `failure` / `partial` |
| `confidence` | Yes | 0.0–1.0 (user-confirmed = 0.9) |
| `solution_steps` | Yes* | *optional when `outcome: "failure"` |
| `environment` | No | omit rather than fill with empty strings |
| `tags` | No | `^[a-z0-9_]+$` format |

---

## Backends

| Backend | When used | Config |
|---|---|---|
| `jsonl` | default / fallback | no config needed |
| `cli` | when `HERMES_KB_CLI_SEARCH` is set | LLM-Wiki CLI wrapper |
| `http` | when `HERMES_KB_BACKEND=http` | `HERMES_KB_HTTP_ENDPOINT` |

---

## Running evals

```bash
SKILL_DIR="$HOME/.hermes/skills/knowledge-distiller"
# Seed the KB with canonical examples first (see references/schema.md §7)
# Then load evals/evals.json and run against your LLM
cat "$SKILL_DIR/evals/evals.json"
```

---

## License

MIT
