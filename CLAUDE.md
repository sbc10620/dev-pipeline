# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Driver CLI

The driver is the only executable component. No build step required — Python 3 standard library only (Python ≥ 3.9, for `argparse.BooleanOptionalAction` used by the `--tdd/--no-tdd` flags).

```bash
# Validate a project's config before running
python3 agents/dev-pipeline-tools/driver.py validate-config --config <project>/.dev-pipeline/dev-pipeline.config.json

# Check state of a running pipeline
python3 agents/dev-pipeline-tools/driver.py status --run <project>/.dev-pipeline/latest

# Manually advance state (normally called by the SKILL, not the user)
python3 agents/dev-pipeline-tools/driver.py advance --run <run_dir>

# Convert codex --json payload to canonical review-result JSON
python3 agents/dev-pipeline-tools/driver.py normalize-review --source codex --in codex-raw.json --out review-result.json

# (TDD) deterministically check a role only touched files it is allowed to
python3 agents/dev-pipeline-tools/driver.py check-boundary --run <run_dir> --role implementation --changed src/a.py tests/t.py

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
- MAJOR bump when an existing install would break (e.g. the 1.1.0 config relocation from project root into `.dev-pipeline/`, or the 2.0.0 TDD-by-default flow change).

## Architecture

This repo is a **Claude Code plugin** — it installs agents and a skill into a target project's `.claude/` directory.

### Execution model

```
User: /dev-pipeline --plan plan.md  [--tdd | --no-tdd]
         │
         ▼
  SKILL.md (thin orchestrator, main session) — reads states/<state>.md per transition
         │
         ├─ python3 driver.py init / advance / validate-result / normalize-review / check-boundary
         │    └─ All state transitions are decided HERE, never by the LLM
         │
         ├─ Agent: dp-test-implementor  (TDD: writes tests from the spec)
         ├─ Agent: dp-implementor       (writes production code)
         ├─ Agent: dp-tester            (runs build/install/test, returns JSON; used by both red_test and test)
         └─ codex-companion.mjs adversarial-review --wait --json
              └─ fallback: Agent dp-reviewer  (returns JSON)
```

### State machine (`driver.py`)

TDD flow (default, `driver.tdd_mode: true`):
`init → test_implementation → red_test → implementation → test → review → done | failed`

Legacy flow (`tdd_mode: false` or `--no-tdd`):
`init → implementation → test → review → done | failed`

TDD is opt-out: `driver.tdd_mode` (config, default true) is overridable per run with `--tdd` / `--no-tdd` (flag > config > default). The chosen value is frozen once into `state.tdd_mode` at init; `state.red_phase` tracks whether the one-time RED gate is still pending.

Key transition rules:
- `init` → `test_implementation` when tdd_mode, else → `implementation`
- `test_implementation` → `red_test` while `red_phase` (first authoring), else → `test` (a test-repair pass)
- `red_test` reuses `dp-tester` but inverts the meaning: a **failing** run = RED confirmed → `implementation` (and `red_phase` flips to false). A **passing** run = RED not confirmed (vacuous tests) → re-author, incrementing `iterations.test_implementation`; `failure_type: environment` halts.
- `implementation` always moves directly to `test` (no result JSON needed)
- `test` fail with `failure_type: environment` → `failed(halt_reason=environment)` immediately (no retry); `failure_type: code` → back to `implementation`, increments `test` counter
- `review` gate is controlled by `driver.review_block_severity` (default: critical+high findings block; `null` for verdict-based). On failure under TDD the driver routes by where the **blocking findings** point: a finding in a `test_paths` file → `test_implementation` (the implementor cannot edit tests); otherwise → `implementation`.
- Counters (`iterations.test`, `iterations.review`, `iterations.test_implementation`) are independent and never reset within a run. The off-by-one is intentional and unchanged: `max_X_iteration = N` permits N+1 attempts (`> max` check).
- Role boundary: `driver check-boundary --role <test_implementation|implementation> --changed <files>` deterministically verifies the test author only touched `test_paths` and the implementor never did (glob matcher owned by the driver; `**` = any depth, `*` = within a segment).

All new keys are read with `.get(default)` so a run/config created by an older driver resumes on the legacy path without crashing. `driver.py` outputs a single JSON object on stdout for every subcommand. All state is stored in `<run_dir>/state.json`.

### Runner abstraction

`config.runners.<role>` is an **ordered array of backends**. SKILL tries them front-to-back with fallback on failure:
- `claude-subagent` — dispatches via Agent tool
- `codex-adversarial-review` — calls `codex-companion.mjs adversarial-review --wait --json`, then `driver normalize-review`
- `bash` — future extension (e.g., cline CLI)

### Codex review integration

Codex is called with `--wait --json`. The spec is passed through codex's focus text (codex has no dedicated spec flag), so it reviews the changes against the spec's Acceptance Criteria. The `payload.result` field maps 1:1 to the `review-result` schema (including `confidence`). `normalize-review` performs the mapping. Fallback is triggered by: plugin not found, non-zero exit, `payload.parseError` present, or `payload.result` absent.

### Key files

| Path | Role |
|---|---|
| `agents/dev-pipeline-tools/driver.py` | State machine — single source of truth for state transitions |
| `agents/dev-pipeline-tools/test/test_driver.py` | Deterministic black-box tests for the driver (CLI subprocess; no LLM) |
| `agents/dev-pipeline-tools/schemas/` | JSON schemas for config, test-result, review-result, state |
| `agents/dev-pipeline-tools/config.example.json` | Seed config (English defaults, placeholders for tester instructions) |
| `claude/agents/dp-implementor.md` | Implementor subagent — production code only; in TDD must not touch `test_paths` |
| `claude/agents/dp-test-implementor.md` | Test author subagent (TDD) — writes tests from the spec, tests only (no Bash), stays within `test_paths` |
| `claude/agents/dp-tester.md` | Tester subagent — exit-code-only pass/fail, classifies `failure_type`; used by both `red_test` and `test` |
| `claude/agents/dp-reviewer.md` | Reviewer subagent — fully read-only (reviews test code too, never runs it), codex fallback |
| `claude/skills/dev-pipeline/SKILL.md` | Thin orchestrator — Global Rules, Step 0, Run Context, state→file index |
| `claude/skills/dev-pipeline/states/<state>.md` | Per-state procedure (progressive disclosure); SKILL reads `states/<next_state>.md` after each `advance` |
| `install.sh` | Copies agents + skill (incl. `states/`) + `config.example.json` into `<project>/.claude/`, updates .gitignore. Does NOT seed the config — the skill bootstraps it on first run. |

### Runtime layout (inside target project, not this repo)

```
<project>/.dev-pipeline/
├── dev-pipeline.config.json   # user config — bootstrapped by the skill (driver bootstrap-config) on first run, lives here (gitignored), NOT in project root
├── latest -> runs/<run-id>
└── runs/<YYYYMMDD-HHMMSS>/
├── state.json           # driver owns this
├── spec.md              # generated from plan at init; shared by test author, implementor + reviewer (TDD: + Test Targets/Interface)
├── attempts.md          # failure log appended on every test_implementation/test/review failure; passed to authors on retry
├── config.snapshot.json
└── iterations/<n>/
    ├── red-test-result.json   # TDD red_test result (validated against the test-result schema)
    ├── test-result.json
    ├── review-result.json
    └── codex-raw.json
```

## Config requirements

`.dev-pipeline/dev-pipeline.config.json` must be present in the target project. It is **bootstrapped by the skill on the first `/dev-pipeline` run** — when the config is absent, the SKILL calls `driver bootstrap-config`, which copies it from the template into the gitignored `.dev-pipeline/` directory (not the project root) and stops so the user can configure it. The three tester instructions are **mandatory and may not contain placeholder values** (`<...>`):

```json
"tester": {
  "build_instruction":   "...",   // or "no build step"
  "install_instruction": "...",   // or "no install step"
  "test_instruction":    "..."    // or "no test step"
}
```

When TDD is enabled (default), `llm.test_implementor` and `runners.test_implementor` are also **required** (placeholders rejected):

```json
"test_implementor": {
  "focus": "...",
  "framework_instruction": "...",        // framework + where/how tests are written
  "test_paths": ["tests/**"]             // globs matching test files only — the role boundary
}
```

`driver validate-config` enforces all of this (and rejects placeholders). The new config keys are **optional in the schema** with code defaults, so a 1.x config still parses — but because `tdd_mode` defaults to true, a config lacking `test_implementor` is rejected unless you add it or set `tdd_mode: false` / pass `--no-tdd`. Use `validate-config --tdd|--no-tdd` to check a config under either mode.

## Testing

Deterministic tests for the state machine live in `agents/dev-pipeline-tools/test/test_driver.py`.
They drive `driver.py` as a CLI subprocess (the way the SKILL does) and assert on state
transitions, the review gate, schema validation, and the auxiliary subcommands. No LLM agent
or codex is invoked; standard library only. After changing `driver.py` or any schema, run:

```bash
python3 agents/dev-pipeline-tools/test/test_driver.py
# or
python3 -m unittest discover -s agents/dev-pipeline-tools/test -v
```

## Schema validation

`driver.py` uses a lightweight built-in validator (no external deps). Schemas live in `agents/dev-pipeline-tools/schemas/`. If you add a new field to a schema, also update the validator's `_validate()` for any type-specific logic (e.g., `oneOf`, `enum`, `minLength`).

## Installation

```bash
bash install.sh <project-dir>
```

Installs into `<project-dir>/.claude/` only (never user-global). `install.sh` copies `driver.py`, the `schemas/` directory, and `config.example.json` into `<project-dir>/.claude/skills/dev-pipeline/` so the installed pipeline runs standalone without the source repo present. `driver.py` resolves both its schemas and the config template relative to its own location (`SCHEMA_DIR` / `EXAMPLE_PATH = pathlib.Path(__file__).parent / ...`), so the copied driver finds them. The SKILL locates the driver as `<skill_dir>/driver.py`. `install.sh` does **not** create the config — `driver bootstrap-config` seeds it from the template on the first run.
