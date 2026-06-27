# Changelog

All notable changes to dev-pipeline are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The version is defined in one place — `__version__` in
`agents/dev-pipeline-tools/driver.py`. Check an installed copy with
`python3 .claude/skills/dev-pipeline/driver.py --version`.

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
