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
  .claude/skills/dev-pipeline/config.example.json  (config template)

This installer does NOT create .dev-pipeline/dev-pipeline.config.json. The skill
bootstraps it from the template on the first /dev-pipeline run (driver
bootstrap-config) and stops so you can fill in the tester instructions. The
config lives inside .dev-pipeline/ (gitignored) so it never clutters the project
root or gets confused with the project's own source files.

The installed .claude/ files are NOT gitignored (their history is tracked, e.g.
for self-evolution). Commit them before running /dev-pipeline so the reviewer
does not mistake them for your changes (the install output explains how).

After the first /dev-pipeline run, edit <project-dir>/.dev-pipeline/dev-pipeline.config.json and fill in:
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
# Display-only path; the skill (driver bootstrap-config) creates this on first run.
RUNTIME_DIR="${PROJECT_DIR}/.dev-pipeline"
GITIGNORE="${PROJECT_DIR}/.gitignore"

# Read the version from driver.py (the single source of truth).
DP_VERSION="$(python3 "${SOURCE_TOOLS}/driver.py" --version 2>/dev/null | awk '{print $2}')"
DP_VERSION="${DP_VERSION:-unknown}"

echo "[dev-pipeline] Installing version ${DP_VERSION} into: ${PROJECT_DIR}"

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

# Copy the config template next to driver.py so `driver bootstrap-config` can
# seed .dev-pipeline/dev-pipeline.config.json on the first /dev-pipeline run.
# This installer intentionally does NOT create the config itself.
if [[ ! -f "${CONFIG_EXAMPLE}" ]]; then
  echo "[dev-pipeline] ERROR: Config template not found: ${CONFIG_EXAMPLE}"
  exit 1
fi
cp "${CONFIG_EXAMPLE}" "${SKILLS_DST}/config.example.json"
echo "[dev-pipeline] Copied: .claude/skills/dev-pipeline/config.example.json"

# Gitignore the runtime directory only.
# The installed machinery under .claude/ is intentionally NOT gitignored: it is
# tracked so its history can be managed (e.g. by self-evolution). To keep the
# reviewer from confusing the installed agents/skill with the user's changes,
# the user must COMMIT the installed .claude/ files before running /dev-pipeline
# (see the post-install notice below). Once committed, they are no longer part
# of the working-tree review scope.
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
echo "[dev-pipeline] Installation complete (version ${DP_VERSION})."
echo "  Check the installed version anytime with:"
echo "    python3 .claude/skills/dev-pipeline/driver.py --version"
echo ""
echo "IMPORTANT: Commit the installed dev-pipeline files BEFORE running /dev-pipeline."
echo "  The review step uses working-tree scope, so any uncommitted/untracked file"
echo "  is treated as part of your change. Committing the installed agents and skill"
echo "  keeps the reviewer focused on your code, not on dev-pipeline's own tooling:"
echo "    git add .claude/agents/dp-implementor.md .claude/agents/dp-tester.md \\"
echo "            .claude/agents/dp-reviewer.md .claude/skills/dev-pipeline/"
echo "    git commit -m \"Add dev-pipeline (agents + skill)\""
echo ""
echo "Next steps:"
echo "  1. Write your plan.md"
echo ""
echo "  2. In Claude Code, run:"
echo "     /dev-pipeline --plan plan.md"
echo "     The first run creates ${RUNTIME_DIR}/dev-pipeline.config.json"
echo "     from the template and stops."
echo ""
echo "  3. Edit ${RUNTIME_DIR}/dev-pipeline.config.json"
echo "     Fill in: llm.tester.build_instruction"
echo "              llm.tester.install_instruction"
echo "              llm.tester.test_instruction"
echo "     Then re-run /dev-pipeline --plan plan.md"
