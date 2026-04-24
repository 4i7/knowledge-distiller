#!/usr/bin/env bash
# install.sh — Install knowledge-distiller into the Hermes-Agent skills directory
#
# Usage:
#   bash install.sh                    # installs to ~/.hermes/skills/knowledge-distiller
#   HERMES_SKILLS_DIR=/custom bash install.sh

set -euo pipefail

DEST="${HERMES_SKILLS_DIR:-$HOME/.hermes/skills}/knowledge-distiller"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "[install] source : $SRC"
echo "[install] target : $DEST"

if [[ -d "$DEST" ]]; then
  echo "[install] existing install found — updating in place"
else
  mkdir -p "$DEST"
fi

# Copy skill files (exclude runtime KB data and git artifacts)
rsync -av --exclude='.git' --exclude='__pycache__' --exclude='*.pyc' \
  --exclude='kb.jsonl' --exclude='index.md' --exclude='records/' \
  "$SRC/" "$DEST/"

# Verify scripts are executable
chmod +x "$DEST/scripts/search_knowledge.py" "$DEST/scripts/update_knowledge.py"

echo ""
echo "[install] Done. Add to your shell profile if not already set:"
echo "  export KNOWLEDGE_DISTILLER_SKILL_DIR=\"$DEST\""
echo ""
echo "[install] If using LLM-Wiki / Obsidian, ensure one of these env vars is set:"
echo "  export WIKI_PATH=\"/path/to/your/obsidian/vault\""
echo "  # or"
echo "  export HERMES_KB_DIR=\"/path/to/custom/kb\""
