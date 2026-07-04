# Changelog

All notable changes to dev-pipeline are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The version is defined in one place — `__version__` in
`agents/dev-pipeline-tools/driver.py`. Check an installed copy with
`python3 .agents/skills/dev-pipeline/driver.py --version`.

## [4.0.0] - 2026-07-03

Make the install layout **provider-neutral and multi-host**. The role prompts
are no longer Claude Code subagents (since 3.0.0 they are just LLM-agnostic prose
the driver assembles), so they move inside the skill, and the skill installs into
the **open Agent Skills standard** location `.agents/skills/dev-pipeline/` — read
natively by Codex, Gemini CLI, Cursor, Kiro, OpenCode and others. Hosts that
don't read that standard yet get their own entry point. **Breaking** — existing
installs must be reinstalled (`bash install.sh <project-dir>`).

### Changed
- **Source layout**: the `claude/` directory is gone. The skill lives at
  `agents/skills/dev-pipeline/`, and the role prompts move from `claude/agents/`
  into the skill's own `agents/` subdir (`agents/skills/dev-pipeline/agents/dp-*.md`).
- **Install layout**: `install.sh` installs the canonical skill into
  `<project>/.agents/skills/dev-pipeline/` (prompts under `agents/`).
  `driver.role_prompt_path` resolves prompts from `<skill_dir>/agents/`, so every
  installed copy is self-contained.
- **Per-host entry points**:
  - **Claude Code** — a **real copy** at `.claude/skills/dev-pipeline/`. Claude Code
    does not read `.agents/skills/` yet (anthropics/claude-code#31005) and won't
    follow a symlinked skill directory (#14836), so a copy is required, not a symlink.
  - **Cline** — a thin pointer at `.clinerules/workflows/dev-pipeline.md` (slash
    `/dev-pipeline.md`) that reads and follows `.agents/skills/dev-pipeline/SKILL.md`.
  - **Codex / Gemini / Cursor / …** — no wiring needed; they discover
    `.agents/skills/` directly.
- **Committing the install**: stage `.agents/skills/dev-pipeline/`,
  `.claude/skills/dev-pipeline/`, and `.clinerules/workflows/dev-pipeline.md`.
  `.agents/` is the single source of truth; the self-evolution commit in `done.md`
  edits `.agents/` and mirrors the change into the `.claude/` copy.

### Fixed
- Upgrading over a pre-4.0.0 install: `install.sh` replaces the old real
  `.claude/skills/dev-pipeline` directory (or a 4.0.0-dev symlink) with the real
  copy and removes the stale `.claude/agents/dp-*.md` prompts.
- The driver now warns on stderr when a role's prose file is missing instead of
  silently running with a stub system prompt.

## [3.0.0] - 2026-06-30

Run every LLM role through a single **bash runner** so the pipeline is
host-agnostic: any LLM can drive the loop, and each stage can use a different
LLM. The Claude-Code subagent path is removed. **Breaking** — existing configs
must be migrated.

### Added
- **`driver run-stage --role <role>`**: the driver deterministically assembles a
  role's prompt from its LLM-agnostic `dp-*.md` (frontmatter stripped → system) +
  the persisted `stage-input.json` (inputs), runs the configured bash runner(s)
  front-to-back, and validates by category — file roles (exit 0), JSON roles
  (result written to `{output_file}` → normalizer → schema), named roles
  (spec_author: required sections / `INSUFFICIENT:` marker). One error-fed retry
  before fallback. Normalizers: `passthrough` / `claude-cli` / `codex-cli`.
- **`dp-spec-author`** role + runner: the spec is now authored by a runner, not
  the orchestrator, so the whole creative path is LLM-agnostic.
- **`driver migrate-config`**: converts a pre-3.0.0 config (claude-subagent /
  codex-adversarial-review runners) to the bash defaults (incl. `spec_author`).
- First principle, documented and enforced: **role `.md` files are LLM-agnostic**
  (no model/tool/CLI references); the LLM, flags, and per-role tool envelope live
  only in `config.runners.<role>`. Swapping/adding an LLM is a config-only change.

### Changed / Breaking
- `config.runners.<role>` items are now `{type:"bash", command, normalizer?,
  timeout?}` only; `claude-subagent` and `codex-adversarial-review` are removed
  from the schema. `runners.spec_author` is now required. `validate-config`
  detects the removed types and points at `migrate-config`.
- `config.example.json` ships per-role bash runners with minimal tool envelopes
  (claude `--allowedTools` by role; codex `exec -s read-only` reviewer + claude
  fallback), validated by a real-CLI spike.
- The SKILL and every state file call `driver run-stage` instead of dispatching a
  subagent / assembling prompts inline; `SKILL.md` drops the `Agent` tool. The git
  baseline/boundary/manifest bookkeeping and the `done` commit are unchanged.
- `review-result.source` gains `bash-runner`; the reviewer no longer claims a
  specific backend (it does not know which LLM runs it).

### Migration
- Run `python3 .claude/skills/dev-pipeline/driver.py migrate-config --config
  <project>/.dev-pipeline/dev-pipeline.config.json`, then review the generated
  runner commands. Update the driver and the installed skill **in lockstep**.
- Requires the `claude` and/or `codex` CLI on PATH for the default runners.
- **Security:** default `claude` runners run headless with pre-approved tools and
  no OS sandbox; `plan.md`/`spec.md`/code are untrusted. Run dev-pipeline in a
  sandboxed/throwaway environment and keep each role's `--allowedTools` minimal
  (read-only roles use a stdout-redirect command with no `Write`). See AGENTS.md
  "Security / trust model".

## [2.3.1] - 2026-07-01

### Fixed
- A **skipped** test stage now validates when the tester emits `command: null`.
  The `test-result` schema required `command` to be a string, so a skipped stage
  (which runs no command) failed schema validation — forcing a needless retry.
  `command` is now nullable (like `exit_code`), and `dp-tester` is told to emit
  `command: null` for skipped stages.

## [2.3.0] - 2026-06-30

The implementor build-checks its code before handing off, so compile errors are
caught early instead of bouncing through the tester.

### Changed
- `dp-implementor` now runs the project's `build_instruction` after implementing
  (skipped for "no build step"), fixes compile errors within its turn (soft cap of
  2–3 rebuilds), and only then hands off. It still must not run the separate
  install/test stages — the tester remains the authoritative build/install/test
  gate. `build_instruction` is now echoed on every transition into `implementation`.

### Notes / limitations
- The implementor's build is a best-effort early check, not a replacement for the
  tester's build (which may run in a cleaner/different environment) — it reduces
  bounces from obvious compile errors but adds a second build per iteration.
- **Keep build output gitignored and outside `test_paths`.** Build artifacts the
  implementor produces are part of its git delta: gitignored untracked files are
  excluded automatically (`--exclude-standard`), but a non-gitignored artifact
  under `test_paths` can trip the role-boundary check, and a build that rewrites a
  *tracked* file (e.g. a lockfile) can land that change in the commit. Gitignore
  build output and do not let `test_paths` overlap the build directory.

## [2.2.0] - 2026-06-30

Harden RED-phase failure classification so an unimplemented feature is not
mistaken for an environment failure, and forbid the orchestrator from silently
editing the user's config.

### Added
- **Config guardrail (Global Rule 10).** The orchestrator must never modify
  `.dev-pipeline/dev-pipeline.config.json` itself, nor let an agent do so. If at
  any point it judges the config needs changing (validation failure, a wrong
  tester instruction, an environment halt, a runner change), it must STOP, propose
  the exact change to the user, and let the user apply/confirm it before
  continuing — never edit-and-proceed. Reinforced in `init`/`failed` states and in
  every write-capable agent (`dp-implementor`, `dp-test-implementor`, `dp-tester`).

### Changed
- `dp-tester` now distinguishes a **missing third-party dependency/toolchain**
  (`environment`) from a **missing first-party symbol that is part of the feature
  under test** (`code`). The latter — e.g. `ModuleNotFoundError` for a module the
  spec defines, or a compile error referencing a not-yet-implemented function — is
  the expected RED signal, not an environment problem.
- The `red_test` state now passes the tester an explicit RED-phase context:
  production is intentionally absent, so import/compile/symbol failures pointing at
  the spec's interface must be classified `code`. This prevents a misclassification
  from halting the run (`red_test` treats `environment` as a hard halt). The driver
  state machine is unchanged — `fail`+`code` still confirms RED → implementation,
  and genuine `environment` failures still halt.

## [2.1.0] - 2026-06-30

Commit only what the pipeline produced, and make every state decision flow
through the driver's echoes instead of the config snapshot.

### Added
- **Change manifest** — a new `driver record-changes` subcommand accumulates the
  files each authoring agent actually wrote (the same per-step delta already used
  for the role-boundary check) into `<run_dir>/changed-manifest.txt`, de-duplicated
  and excluding `.dev-pipeline/` artifacts. The `done` commit now stages **only**
  manifest paths (with `git add -A -- <path>` so deletions are committed too)
  instead of `git add -A`, so untracked junk **not produced by the authoring
  agents themselves** (cscope, ctags, and build/test caches — the latter are
  generated in the separate `test` state, after the delta snapshot) no longer
  lands in the commit — no per-run `.gitignore` upkeep required. The `review`
  state's dp-reviewer fallback scopes to the manifest as well.
- `tdd_mode` and every config-derived value a destination state needs
  (`design_instruction`, `test_paths`, the per-role runner arrays,
  `run_self_evolution`) are now echoed by **every** `driver advance`. State files
  read these echoes; reading `config.snapshot.json` for control flow is now
  forbidden (new Global Rule 9). This removes a class of resume/compaction bugs —
  notably recovering `tdd_mode` from `config.snapshot.driver.tdd_mode`, which is
  wrong under a `--tdd`/`--no-tdd` override (the authoritative value is the frozen
  `state.tdd_mode`).

### Changed
- The per-agent delta is now computed `project_root`-relative
  (`git -C <project_root> diff --name-only --relative` + `ls-files --others`) so
  the manifest, boundary check, and commit agree on one path base even when the
  config lives in a repo subdirectory.
- In legacy (`--no-tdd`) runs the `implementation` state now also stages a baseline
  and records the manifest, so non-TDD commits get the same junk filtering.

### Compatibility
- MINOR bump: no driver API breaks. A run started by an older driver (no manifest)
  resumes fine — `done` falls back to the legacy `git add -A` flow and warns. All
  newly echoed values are read with `.get(default)`.
- **Update the driver and the skill in lockstep.** A partial install (new
  `states/*.md` against an old `driver.py`) would call `record-changes` on a driver
  that lacks it; the call fails, no manifest is written, and `done` silently falls
  back to `git add -A` — re-admitting junk. `install.sh` copies both together, so
  this only affects manual partial updates.

## [2.0.0] - 2026-06-29

Test-Driven Development support. The pipeline can now author tests from the spec
and prove they fail (RED) before writing code, then make them pass (GREEN).

### Added
- **TDD flow** (default): `init → test_implementation → red_test → implementation
  → test → review → done`. New states `test_implementation` (a new
  `dp-test-implementor` agent writes tests from the spec) and `red_test` (the
  existing `dp-tester` proves those tests fail; a failing run is the success
  condition). Disable per run with `--no-tdd` or `driver.tdd_mode: false`.
- `driver.tdd_mode` (config, default true) and `--tdd` / `--no-tdd` flags on
  `init` and `validate-config` (precedence: flag > config > default). The
  resolved value is frozen into `state.tdd_mode`; `state.red_phase` tracks the
  one-time RED gate.
- `llm.test_implementor` config (`focus`, `framework_instruction`, `test_paths`)
  and `runners.test_implementor`; `driver.max_test_implementation_iteration`
  (default 2) bounds re-authoring when RED is not confirmed.
- New `check-boundary` subcommand + role guard: the test author may only touch
  `test_paths`, the implementor may never touch them. The driver owns a
  deterministic glob matcher (`**` = any depth, `*` = within a segment).
- Review-failure routing is finding-aware under TDD: a blocking finding in a
  test file routes back to `test_implementation`; production findings route to
  `implementation`.
- `spec.md` gains a `Test Targets / Interface` section under TDD, and the init
  state requires Acceptance Criteria concrete enough to test (it stops and asks
  rather than fabricating tests for a too-vague plan).
- New `dp-test-implementor.md` agent; `TestTDD`, `TestUpgradeSafety`, and
  `TestCheckBoundary` suites in `test_driver.py`.

### Changed
- **BREAKING:** TDD is on by default. Upgrading a 1.x install: the new config
  keys are optional in the schema (code-level defaults), but because `tdd_mode`
  defaults to true a config without `llm.test_implementor` is rejected at
  `validate-config`/`init` — add the `test_implementor` block, or set
  `tdd_mode: false` / run `--no-tdd` to keep the legacy flow. Refresh the
  installed schema by re-running `install.sh`.
- SKILL.md is now a thin orchestrator (Global Rules, Step 0, Run Context,
  state→file index); each state's procedure lives in
  `claude/skills/dev-pipeline/states/<state>.md` and is read on demand. The
  driver echoes the per-step `iter_dir` so each state file is self-contained.
- `dp-reviewer.md` clarifies that read-only means "never *run* tests" — test
  source code is in review scope, and a test that contradicts the spec is a
  legitimate high-severity finding (test style/coverage nitpicks stay ≤ medium).
  This guidance is also in the default `reviewer.focus` so the codex path sees it.
- `dp-implementor.md` must not create/modify `test_paths` files under TDD.
- `install.sh` installs `dp-test-implementor.md` and the `states/` directory.
- All new state/iteration keys are read with `.get(default)`, so a run created
  by an older driver resumes on the legacy path without crashing.

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
