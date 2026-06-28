# Changelog

All notable changes to dev-pipeline are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The version is defined in one place — `__version__` in
`agents/dev-pipeline-tools/driver.py`. Check an installed copy with
`python3 .claude/skills/dev-pipeline/driver.py --version`.

## [1.3.0] - 2026-06-28

### Added
- Done-state retrospective (SKILL Step 5.3) now reports the model running the
  orchestrator (main session) and, for each state, the runner/method that
  actually carried out the work (claude-subagent + agent name, bash command, or
  codex vs dp-reviewer fallback), making a finished run's execution path
  traceable from the retrospective alone.
- The codex reviewer now receives the spec through the focus text (codex has no
  dedicated spec flag), so it can review changes against the spec's Acceptance
  Criteria instead of focus text alone.

### Changed
- `dp-tester.md` / `dp-reviewer.md`: the JSON example in each agent is now the
  single source of truth for output shape; rules no longer re-list keys or tell
  the tester to read `test-result.schema.json`. Field-level constraints and
  enum meanings (per-stage `status`, `severity`) are defined once, reducing the
  risk of emitting placeholder strings like `"pass or fail"`.
- SKILL Step 3 no longer passes a schema path to the tester — the driver still
  enforces the shape via `validate-result`.
- SKILL Step 4.4 reviewer instruction references only the spec (the reviewer
  reads spec.md, never the plan).
- SKILL Step 5.4 spells out exactly which installed files self-evolution may
  edit and commit (`.claude/agents/dp-*.md`, `.claude/skills/dev-pipeline/SKILL.md`).
- `README.md` and `CLAUDE.md` updated to note that the spec is passed to the
  codex reviewer and that self-evolution may also update `SKILL.md`.

### Removed
- `dp-reviewer.md`: dropped the redundant "fallback reviewer" sentence (no
  behavioral effect; already stated in the frontmatter description).

## [1.2.0] - 2026-06-28

### Added
- `driver.py bootstrap-config [--project <dir>]` — seeds
  `.dev-pipeline/dev-pipeline.config.json` from the template when it is absent.
  The driver detects the project root (git top-level, else cwd), creates the
  directory, copies the template, and idempotently adds `.dev-pipeline/` to
  `.gitignore` (in a git repo). Emits a JSON object with
  `status` (`created`/`exists`), `config_path`, and `required_fields`. Existing
  configs are never overwritten.

### Changed
- Config seeding moved from `install.sh` into the skill. On the first
  `/dev-pipeline` run, the SKILL calls `driver bootstrap-config` when no config
  is found, then stops so the user can fill in the tester instructions and
  re-run. This makes the pipeline self-bootstrapping for anyone who obtains the
  repo without knowing about `install.sh`.
- `install.sh` no longer creates `.dev-pipeline/dev-pipeline.config.json`.
  Instead it copies `config.example.json` next to the installed `driver.py`
  (`.claude/skills/dev-pipeline/config.example.json`) so `bootstrap-config` can
  find the template standalone. It still adds `.dev-pipeline/` to `.gitignore`.

## [1.1.1] - 2026-06-27

### Added
- Deterministic test suite for `driver.py` in `agents/dev-pipeline-tools/test/`.
  Drives the driver as a CLI subprocess (as the SKILL does) and verifies state
  transitions, the review gate (severity and verdict modes), schema validation,
  and the auxiliary subcommands — without invoking any LLM agent or codex.
  Standard library only. Run with
  `python3 agents/dev-pipeline-tools/test/test_driver.py`.

## [1.1.0] - 2026-06-27

### Added
- `driver.py --version` (also `-V` / `version`) prints the dev-pipeline version.
- `dev_pipeline_version` is recorded in each run's `state.json` for audit.
- `install.sh` prints the version it installs and how to check it afterward.

### Changed
- The implementor and reviewer agents now receive **file paths** (plan, spec,
  attempts, diff) instead of inlined file contents; agents read large files
  themselves via the Read tool. Small config strings remain inline.
- `dev-pipeline.config.json` moved from the project root into the gitignored
  `.dev-pipeline/` directory, so it no longer clutters the project root or gets
  confused with the project's own source files.
  **Note:** this is a relocation — copies installed from 1.0.0 keep their config
  at the project root. Re-run `install.sh` (or move the config) to adopt 1.1.0.
- The installed `.claude/` machinery is intentionally NOT gitignored (its history
  is tracked, e.g. for self-evolution). `install.sh` instead instructs the user
  to commit the installed agents + skill before running, so they stay out of the
  working-tree review scope.

### Fixed
- Test state: the tester now receives the authoritative schema path and the exact
  allowed keys, and on schema-validation failure the orchestrator re-dispatches to
  the tester instead of running build/install/test itself (which violated the
  delegation rule). `dp-tester.md` forbids inventing fields like `failure_stage`.
- Review scope: documented gitignoring build outputs, and stopped the installed
  dev-pipeline files from being swept into the working-tree review scope.

## [1.0.0] - 2026-06-27

### Added
- Initial release: automated **implement → test → review** loop for Claude Code.
- Deterministic state machine in `driver.py` (Python 3 stdlib only).
- Specialized agents: `dp-implementor`, `dp-tester`, `dp-reviewer`.
- Orchestrator skill `dev-pipeline` (`/dev-pipeline --plan <path>`).
- Codex adversarial-review as primary reviewer with `dp-reviewer` fallback.
- Pluggable runner abstraction (ordered backends with fallback).
- `attempts.md` oscillation prevention; environment-vs-code failure routing.
- `install.sh` copies driver + schemas for standalone operation.
