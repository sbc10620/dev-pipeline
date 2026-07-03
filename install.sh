#!/usr/bin/env bash
# dev-pipeline installer — installs the skill into the provider-neutral
# .agents/skills/ tree (the open Agent Skills standard) and wires up each host
# that needs its own entry point (Claude Code, Cline).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<EOF
Usage: bash install.sh <project-dir>

Installs dev-pipeline once and wires it up for multiple agent hosts.

Canonical install (the open Agent Skills standard — read natively by Codex,
Gemini CLI, Cursor, Kiro, OpenCode, OpenClaw, …):
  .agents/skills/dev-pipeline/SKILL.md
  .agents/skills/dev-pipeline/states/                (per-state orchestration files)
  .agents/skills/dev-pipeline/agents/dp-*.md         (LLM-agnostic role prompts)
  .agents/skills/dev-pipeline/driver.py
  .agents/skills/dev-pipeline/schemas/               (JSON schemas)
  .agents/skills/dev-pipeline/config.example.json    (config template)

Per-host entry points (added because these hosts don't read .agents/skills yet):
  .claude/skills/dev-pipeline/          real copy — Claude Code doesn't read
                                        .agents/skills (anthropics/claude-code#31005)
                                        and won't follow a symlinked skill dir (#14836)
  .clinerules/workflows/dev-pipeline.md thin pointer — Cline slash-workflow

Codex (and other Agent-Skills hosts) need no extra wiring — they discover
.agents/skills/ directly, and the root AGENTS.md is already loaded as guidance.

This installer does NOT create .dev-pipeline/dev-pipeline.config.json. The skill
bootstraps it from the template on the first /dev-pipeline run (driver
bootstrap-config) and stops so you can fill in the tester instructions. The
config lives inside .dev-pipeline/ (gitignored) so it never clutters the project
root or gets confused with the project's own source files.

The installed files are NOT gitignored (their history is tracked, e.g. for
self-evolution). Commit them before running /dev-pipeline so the reviewer does
not mistake them for your changes (the install output explains how).

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
AGENTS_DIR="${PROJECT_DIR}/.agents"
SKILLS_DST="${AGENTS_DIR}/skills/dev-pipeline"
PROMPTS_DST="${SKILLS_DST}/agents"
SOURCE_SKILL="${SCRIPT_DIR}/agents/skills/dev-pipeline"
SOURCE_AGENTS="${SOURCE_SKILL}/agents"
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
mkdir -p "${SKILLS_DST}/schemas" "${SKILLS_DST}/states" "${PROMPTS_DST}"

# Copy role-prompt files (LLM-agnostic prose; run-stage assembles them into the
# system prompt). They live inside the skill (agents/) — no longer a top-level
# .claude/agents/ subagent dir.
for f in dp-spec-author.md dp-implementor.md dp-test-implementor.md dp-tester.md dp-reviewer.md; do
  src="${SOURCE_AGENTS}/${f}"
  if [[ ! -f "$src" ]]; then
    echo "[dev-pipeline] ERROR: Source file not found: ${src}"
    exit 1
  fi
  cp "${src}" "${PROMPTS_DST}/${f}"
  echo "[dev-pipeline] Copied: .agents/skills/dev-pipeline/agents/${f}"
done

# Copy skill
cp "${SOURCE_SKILL}/SKILL.md" "${SKILLS_DST}/SKILL.md"
echo "[dev-pipeline] Copied: .agents/skills/dev-pipeline/SKILL.md"

# Copy per-state orchestration files (the SKILL reads states/<state>.md per transition)
for f in init test_implementation red_test implementation test review done failed; do
  src="${SOURCE_SKILL}/states/${f}.md"
  if [[ ! -f "$src" ]]; then
    echo "[dev-pipeline] ERROR: Source file not found: ${src}"
    exit 1
  fi
  cp "${src}" "${SKILLS_DST}/states/${f}.md"
done
echo "[dev-pipeline] Copied: .agents/skills/dev-pipeline/states/ (8 files)"

# Copy driver script (must be co-located with schemas for standalone operation)
cp "${SOURCE_TOOLS}/driver.py" "${SKILLS_DST}/driver.py"
echo "[dev-pipeline] Copied: .agents/skills/dev-pipeline/driver.py"

# Copy schemas (driver.py expects schemas/ in the same directory)
for f in config.schema.json test-result.schema.json review-result.schema.json state.schema.json; do
  src="${SOURCE_TOOLS}/schemas/${f}"
  if [[ ! -f "$src" ]]; then
    echo "[dev-pipeline] ERROR: Schema file not found: ${src}"
    exit 1
  fi
  cp "${src}" "${SKILLS_DST}/schemas/${f}"
done
echo "[dev-pipeline] Copied: .agents/skills/dev-pipeline/schemas/ (4 files)"

# Copy the config template next to driver.py so `driver bootstrap-config` can
# seed .dev-pipeline/dev-pipeline.config.json on the first /dev-pipeline run.
# This installer intentionally does NOT create the config itself.
if [[ ! -f "${CONFIG_EXAMPLE}" ]]; then
  echo "[dev-pipeline] ERROR: Config template not found: ${CONFIG_EXAMPLE}"
  exit 1
fi
cp "${CONFIG_EXAMPLE}" "${SKILLS_DST}/config.example.json"
echo "[dev-pipeline] Copied: .agents/skills/dev-pipeline/config.example.json"

# --- Claude Code entry point: a REAL copy under .claude/skills/ ---
# Claude Code does not read .agents/skills yet (anthropics/claude-code#31005) and
# its skill discovery does not follow a symlinked skill directory (#14836), so we
# mirror the canonical tree as real files. Replace any prior install at the
# destination first (a pre-4.0.0 real dir, or a 4.0.0-dev symlink) so we never
# nest inside it or leave stale files behind.
CLAUDE_SKILL="${CLAUDE_DIR}/skills/dev-pipeline"
mkdir -p "${CLAUDE_DIR}/skills"
if [[ -L "$CLAUDE_SKILL" || -e "$CLAUDE_SKILL" ]]; then
  rm -rf "$CLAUDE_SKILL"
fi
cp -R "${SKILLS_DST}" "$CLAUDE_SKILL"
echo "[dev-pipeline] Copied: .claude/skills/dev-pipeline/ (real copy for Claude Code)"

# Remove stale prompts from pre-4.0.0 installs (they used to live in .claude/agents/).
# They are inert now (the driver only looks inside the skill) but would linger as
# dead tracked files that contradict the "prompts live in the skill" layout.
if [[ -d "${CLAUDE_DIR}/agents" ]]; then
  rm -f "${CLAUDE_DIR}/agents/"dp-spec-author.md "${CLAUDE_DIR}/agents/"dp-implementor.md \
        "${CLAUDE_DIR}/agents/"dp-test-implementor.md "${CLAUDE_DIR}/agents/"dp-tester.md \
        "${CLAUDE_DIR}/agents/"dp-reviewer.md
  rmdir --ignore-fail-on-non-empty "${CLAUDE_DIR}/agents" 2>/dev/null || true
  echo "[dev-pipeline] Cleaned up stale pre-4.0.0 prompts from .claude/agents/ (if any)"
fi

# --- Cline entry point: a thin slash-workflow pointer ---
# Cline discovers /-workflows from .clinerules/workflows/*.md. Rather than
# duplicating the skill, point Cline at the canonical .agents/ copy so there is
# no drift.
CLINE_WF_DIR="${PROJECT_DIR}/.clinerules/workflows"
mkdir -p "${CLINE_WF_DIR}"
cat > "${CLINE_WF_DIR}/dev-pipeline.md" <<'EOF'
# dev-pipeline

Run the dev-pipeline orchestrator: read `.agents/skills/dev-pipeline/SKILL.md`
and follow its instructions exactly, treating any following text as the
arguments (e.g. `--plan plan.md [--tdd | --no-tdd]`).

Arguments: $ARGUMENTS
EOF
echo "[dev-pipeline] Wrote: .clinerules/workflows/dev-pipeline.md (Cline slash-workflow pointer)"

# Gitignore the runtime directory only.
# The installed machinery under .agents/ is intentionally NOT gitignored: it is
# tracked so its history can be managed (e.g. by self-evolution). To keep the
# reviewer from confusing the installed agents/skill with the user's changes,
# the user must COMMIT the installed files before running /dev-pipeline
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
echo "  Codex, Gemini CLI, Cursor and other Agent-Skills hosts pick up the skill"
echo "  from .agents/skills/ automatically. Cline sees it as the /dev-pipeline.md"
echo "  workflow. Claude Code uses the real copy under .claude/skills/."
echo ""
echo "IMPORTANT: Commit the installed dev-pipeline files BEFORE running /dev-pipeline."
echo "  The review step uses working-tree scope, so any uncommitted/untracked file"
echo "  is treated as part of your change. Committing the installed files keeps the"
echo "  reviewer focused on your code, not on dev-pipeline's own tooling:"
echo "    git add .agents/skills/dev-pipeline/ .claude/skills/dev-pipeline/ .clinerules/workflows/dev-pipeline.md"
echo "    git commit -m \"Add dev-pipeline (skill + prompts)\""
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
echo "     Fill in: llm.tester.build_instruction / install_instruction / test_instruction"
echo "     TDD is on by default, so also fill: llm.test_implementor.framework_instruction"
echo "       and llm.test_implementor.test_paths (or run with --no-tdd to skip)."
echo "     The default runners call the 'claude' (and 'codex') CLI — see config.runners."
echo "     SECURITY: default runners run headless without a sandbox and treat plan/spec"
echo "       as untrusted; run dev-pipeline in a sandboxed/throwaway environment."
echo "     Then re-run /dev-pipeline --plan plan.md"
