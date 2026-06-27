# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Driver CLI

The driver is the only executable component. No build step required — pure Python 3 standard library.

```bash
# Validate a project's config before running
python3 agents/dev-pipeline-tools/driver.py validate-config --config <project>/.dev-pipeline/dev-pipeline.config.json

# Check state of a running pipeline
python3 agents/dev-pipeline-tools/driver.py status --run <project>/.dev-pipeline/latest

# Manually advance state (normally called by the SKILL, not the user)
python3 agents/dev-pipeline-tools/driver.py advance --run <run_dir>

# Convert codex --json payload to canonical review-result JSON
python3 agents/dev-pipeline-tools/driver.py normalize-review --source codex --in codex-raw.json --out review-result.json

# Print version
python3 agents/dev-pipeline-tools/driver.py --version

# Full usage
python3 agents/dev-pipeline-tools/driver.py --help
```

## Versioning

- **Single source of truth:** `__version__` in `agents/dev-pipeline-tools/driver.py`. SemVer.
- `install.sh` and `state.json` (`dev_pipeline_version`) **read** this value — never hardcode the version elsewhere.
- To cut a release: bump `__version__`, add a `CHANGELOG.md` section, commit, then `git tag -a <version>`.
- An installed copy self-reports with `python3 .claude/skills/dev-pipeline/driver.py --version` (the install is a copy, so this is how you tell a stale install from the current source).
- MAJOR bump when an existing install would break (e.g. the 1.1.0 config relocation from project root into `.dev-pipeline/`).

## Architecture

This repo is a **Claude Code plugin** — it installs agents and a skill into a target project's `.claude/` directory.

### Execution model

```
User: /dev-pipeline --plan plan.md
         │
         ▼
  SKILL.md (orchestrator, runs in the main Claude session)
         │
         ├─ python3 driver.py init / advance / validate-result / normalize-review
         │    └─ All state transitions are decided HERE, never by the LLM
         │
         ├─ Agent: dp-implementor  (writes code)
         ├─ Agent: dp-tester       (runs build/install/test, returns JSON)
         └─ codex-companion.mjs adversarial-review --wait --json
              └─ fallback: Agent dp-reviewer  (returns JSON)
```

### State machine (`driver.py`)

States: `init → implementation → test → review → done | failed`

Key transition rules:
- `implementation` always moves directly to `test` (no result JSON needed)
- `test` fail with `failure_type: environment` → `failed(halt_reason=environment)` immediately (no retry)
- `test` fail with `failure_type: code` → back to `implementation`, increments `test` counter
- `review` gate is controlled by `driver.review_block_severity` (default: critical+high findings block); set to `null` for verdict-based gating
- Both counters (`iterations.test`, `iterations.review`) are independent and never reset within a run

`driver.py` outputs a single JSON object on stdout for every subcommand. All state is stored in `<run_dir>/state.json`.

### Runner abstraction

`config.runners.<role>` is an **ordered array of backends**. SKILL tries them front-to-back with fallback on failure:
- `claude-subagent` — dispatches via Agent tool
- `codex-adversarial-review` — calls `codex-companion.mjs adversarial-review --wait --json`, then `driver normalize-review`
- `bash` — future extension (e.g., cline CLI)

### Codex review integration

Codex is called with `--wait --json`. The `payload.result` field maps 1:1 to the `review-result` schema (including `confidence`). `normalize-review` performs the mapping. Fallback is triggered by: plugin not found, non-zero exit, `payload.parseError` present, or `payload.result` absent.

### Key files

| Path | Role |
|---|---|
| `agents/dev-pipeline-tools/driver.py` | State machine — single source of truth for state transitions |
| `agents/dev-pipeline-tools/schemas/` | JSON schemas for config, test-result, review-result, state |
| `agents/dev-pipeline-tools/config.example.json` | Seed config (English defaults, placeholders for tester instructions) |
| `claude/agents/dp-implementor.md` | Implementor subagent — reads attempts.md to avoid repeating failed approaches |
| `claude/agents/dp-tester.md` | Tester subagent — exit-code-only pass/fail, classifies `failure_type` |
| `claude/agents/dp-reviewer.md` | Reviewer subagent — fully read-only, codex fallback |
| `claude/skills/dev-pipeline/SKILL.md` | Orchestrator skill — step-by-step workflow with checklists per state |
| `install.sh` | Copies agents+skill into `<project>/.claude/`, seeds config, updates .gitignore |

### Runtime layout (inside target project, not this repo)

```
<project>/.dev-pipeline/
├── dev-pipeline.config.json   # user config — seeded by install.sh, lives here (gitignored), NOT in project root
├── latest -> runs/<run-id>
└── runs/<YYYYMMDD-HHMMSS>/
├── state.json           # driver owns this
├── spec.md              # generated from plan at init; shared by implementor + reviewer
├── attempts.md          # failure log appended on every test/review failure; passed to implementor on retry
├── config.snapshot.json
└── iterations/<n>/
    ├── test-result.json
    ├── review-result.json
    └── codex-raw.json
```

## Config requirements

`.dev-pipeline/dev-pipeline.config.json` must be present in the target project (seeded by `install.sh` inside the gitignored `.dev-pipeline/` directory, not the project root). The three tester instructions are **mandatory and may not contain placeholder values** (`<...>`):

```json
"tester": {
  "build_instruction":   "...",   // or "no build step"
  "install_instruction": "...",   // or "no install step"
  "test_instruction":    "..."    // or "no test step"
}
```

`driver validate-config` enforces this and rejects placeholder values.

## Schema validation

`driver.py` uses a lightweight built-in validator (no external deps). Schemas live in `agents/dev-pipeline-tools/schemas/`. If you add a new field to a schema, also update the validator's `_validate()` for any type-specific logic (e.g., `oneOf`, `enum`, `minLength`).

## Installation

```bash
bash install.sh <project-dir>
```

Installs into `<project-dir>/.claude/` only (never user-global). `install.sh` copies `driver.py` and the `schemas/` directory into `<project-dir>/.claude/skills/dev-pipeline/` so the installed pipeline runs standalone without the source repo present. `driver.py` resolves its schemas relative to its own location (`SCHEMA_DIR = pathlib.Path(__file__).parent / "schemas"`), so the copied driver finds the copied schemas. The SKILL locates the driver as `<skill_dir>/driver.py`.
