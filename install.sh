#!/usr/bin/env bash
# dev-pipeline installer — copies all components into the target project's local .claude/

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<EOF
Usage: bash install.sh <project-dir>

Installs dev-pipeline into <project-dir>/.claude/ (local only, not user-global).

What gets installed:
  .claude/agents/dp-implementor.md
  .claude/agents/dp-tester.md
  .claude/agents/dp-reviewer.md
  .claude/skills/dev-pipeline/SKILL.md
  .claude/skills/dev-pipeline/driver.py
  .claude/skills/dev-pipeline/schemas/  (JSON schemas)
  dev-pipeline.config.json (seeded from config.example.json, if not already present)

After installation, edit <project-dir>/dev-pipeline.config.json and fill in:
  llm.tester.build_instruction
  llm.tester.install_instruction
  llm.tester.test_instruction
EOF
}

if [[ $# -lt 1 ]] || [[ "$1" == "--help" ]] || [[ "$1" == "-h" ]]; then
  usage
  exit 0
fi

PROJECT_DIR="$(cd "$1" && pwd)"
CLAUDE_DIR="${PROJECT_DIR}/.claude"
AGENTS_DST="${CLAUDE_DIR}/agents"
SKILLS_DST="${CLAUDE_DIR}/skills/dev-pipeline"
SOURCE_AGENTS="${SCRIPT_DIR}/claude/agents"
SOURCE_SKILL="${SCRIPT_DIR}/claude/skills/dev-pipeline"
SOURCE_TOOLS="${SCRIPT_DIR}/agents/dev-pipeline-tools"
CONFIG_EXAMPLE="${SOURCE_TOOLS}/config.example.json"
CONFIG_DST="${PROJECT_DIR}/dev-pipeline.config.json"
GITIGNORE="${PROJECT_DIR}/.gitignore"

echo "[dev-pipeline] Installing into: ${PROJECT_DIR}"

# Create destination directories
mkdir -p "${AGENTS_DST}" "${SKILLS_DST}/schemas"

# Copy agent files
for f in dp-implementor.md dp-tester.md dp-reviewer.md; do
  src="${SOURCE_AGENTS}/${f}"
  if [[ ! -f "$src" ]]; then
    echo "[dev-pipeline] ERROR: Source file not found: ${src}"
    exit 1
  fi
  cp "${src}" "${AGENTS_DST}/${f}"
  echo "[dev-pipeline] Copied: .claude/agents/${f}"
done

# Copy skill
cp "${SOURCE_SKILL}/SKILL.md" "${SKILLS_DST}/SKILL.md"
echo "[dev-pipeline] Copied: .claude/skills/dev-pipeline/SKILL.md"

# Copy driver script (must be co-located with schemas for standalone operation)
cp "${SOURCE_TOOLS}/driver.py" "${SKILLS_DST}/driver.py"
echo "[dev-pipeline] Copied: .claude/skills/dev-pipeline/driver.py"

# Copy schemas (driver.py expects schemas/ in the same directory)
for f in config.schema.json test-result.schema.json review-result.schema.json state.schema.json; do
  src="${SOURCE_TOOLS}/schemas/${f}"
  if [[ ! -f "$src" ]]; then
    echo "[dev-pipeline] ERROR: Schema file not found: ${src}"
    exit 1
  fi
  cp "${src}" "${SKILLS_DST}/schemas/${f}"
done
echo "[dev-pipeline] Copied: .claude/skills/dev-pipeline/schemas/ (4 files)"

# Seed config (only if not already present)
if [[ ! -f "${CONFIG_DST}" ]]; then
  cp "${CONFIG_EXAMPLE}" "${CONFIG_DST}"
  echo "[dev-pipeline] Created: dev-pipeline.config.json (from example)"
else
  echo "[dev-pipeline] Skipped: dev-pipeline.config.json already exists"
fi

# Add .dev-pipeline/ to .gitignore
GITIGNORE_ENTRY=".dev-pipeline/"
if [[ -f "${GITIGNORE}" ]]; then
  if grep -qxF "${GITIGNORE_ENTRY}" "${GITIGNORE}" 2>/dev/null; then
    echo "[dev-pipeline] .gitignore: .dev-pipeline/ already present"
  else
    printf '\n# dev-pipeline runtime directory\n%s\n' "${GITIGNORE_ENTRY}" >> "${GITIGNORE}"
    echo "[dev-pipeline] Updated: .gitignore (added .dev-pipeline/)"
  fi
else
  printf '# dev-pipeline runtime directory\n%s\n' "${GITIGNORE_ENTRY}" > "${GITIGNORE}"
  echo "[dev-pipeline] Created: .gitignore (with .dev-pipeline/)"
fi

echo ""
echo "[dev-pipeline] Installation complete."
echo ""
echo "Next steps:"
echo "  1. Edit ${CONFIG_DST}"
echo "     Fill in: llm.tester.build_instruction"
echo "              llm.tester.install_instruction"
echo "              llm.tester.test_instruction"
echo ""
echo "  2. Write your plan.md"
echo ""
echo "  3. In Claude Code, run:"
echo "     /dev-pipeline --plan plan.md"
