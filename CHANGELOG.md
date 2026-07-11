# Changelog

All notable changes to dev-pipeline are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The version is defined in one place — `__version__` in
`agents/dev-pipeline-tools/driver.py`. Check an installed copy with
`python3 .agents/skills/dev-pipeline/driver.py --version`.

## [6.1.1] - 2026-07-11

### Removed
- **The review-result `source` field.** It was write-only provenance: no state file read it, `done.md` never surfaced it, and it duplicated `config.snapshot.json`'s `runners.reviewer[0].type`. It was also the root cause of a correctness bug — `dp-reviewer.md` told the role to always emit `"source": "bash-runner"`, and the driver only corrected it to the true execution mode inside `finalize-stage`/`run-stage`'s `judge()`; if that stamp step was ever skipped (e.g. a `subagent`/`main-session` reviewer whose `finalize-stage` call didn't run), the false `bash-runner` value passed `advance`'s schema re-validation undetected, because it was a valid enum member. Removing the field removes the possibility of a false value entirely. `review-result.schema.json` drops `source` from both `required` and `properties`; `dp-reviewer.md`'s example output and checklist no longer mention it; `_finalize_json` no longer takes a `source` argument.

## [6.1.0] - 2026-07-10

Re-introduces `--resume`, removes an orphaned subcommand, and folds Karpathy-style discipline into the authoring role prompts.

### Added
- **`--resume [<run_dir>]` / `driver resume`** — continue an **interrupted** run from the state it stopped in, without re-running `init` (which starts a NEW run) or redoing completed stages. Every `advance` now persists its full landing echo to `<run_dir>/last-advance.json` (written **before** `state.json`, so a crash between the two is disambiguable); `driver resume` replays it. Handles: parked-at-init (→ advance), normal replay (re-emits the exact echo incl. runner arrays + re-persists a byte-identical `stage-input.json`), the crash window (advance died before persisting → re-run advance, idempotent), terminal states (replays the full `done`/`failed` echo for idempotent finalization), a best-effort `possibly_live` concurrency flag, and a precise manual recipe for a run with no `last-advance.json`. `states/resume.md` adds the SKILL-side delta recovery (worktree-vs-index minus the manifest, boundary-checked, `record-changes`d) so a pre-crash authoring edit is never silently dropped. Ported from the parked 5.3.0 branch and adapted to the 6.0.0 flow (no header; attempts are auto-recorded by advance).
- **Karpathy-style discipline in the authoring prompts** — `dp-implementor.md` sharpens its scope rule to **minimum, surgical changes** (nothing speculative; touch only what the contract requires; do not refactor unrelated code or delete pre-existing dead code) and to **surface assumptions instead of guessing** when the contract is ambiguous. `dp-test-implementor.md` frames each test as the **executable success criterion** for its Acceptance Criterion and adds a **test-only-what-is-specified** rule (no speculative/redundant tests).

### Removed
- **The orphaned `normalize-review --source codex` subcommand** — its only caller, the pre-3.0.0 `codex-adversarial-review` runner type, was removed in 3.0.0; no state file, runner, or doc referenced it. (A codex reviewer is still fully supported as a bash runner emitting plain review-result JSON through the `default`/`passthrough` normalizer.)

### Changed
- Conservative MD pass: trimmed a couple of clearly-redundant restatements (the files are deliberately dense LLM-executed prose, so load-bearing rules/checklists/structure were preserved).
- Provider-neutrality: replaced the Claude-Code-specific `/advisor` reference in `done.md`/`dp-tester.md` with host-agnostic wording ("a dedicated advisory/code-review capability").

## [6.0.0] - 2026-07-10

Config/plan flow redesign: the `plan.md` config header is **removed**, config lives solely in `config.json` and is written by a new conversational **`--update-config`** flow, and `main-session`/`subagent` handoffs get a firm single-role persona preamble. Also: `advance` records retry context to `attempts.md` itself (the `append-attempt` subcommand is gone), and the LLM-named `claude-cli`/`codex-cli` normalizers are replaced by a single `default`. Folds in the reviewer-independence guidance drafted for 5.4.1. **Breaking** — existing installs must reconfigure.

### Removed (breaking)
- **The `plan.md` `dev-pipeline-config` header is gone.** `plan.md` is now a pure spec body (Requirements, Acceptance Criteria, Interface). All header machinery is deleted: `parse_plan_header`, `merge_plan_header`, the `_HEADER_PROSE_KEYS`/`_HEADER_EXEC_KEYS` whitelists, the `--header-approved` flag (on `init` and `validate-config`), and the `driver.allow_unattended_header_merge` opt-in. `init` now reads the whole plan file as the contract and snapshots `config.json` verbatim; there is nothing untrusted to merge, which **simplifies the trust model** (the plan carries no config).
- **`driver set-runners` is replaced by `driver apply-config`** (see Added). The one-time, runners-only, refuse-when-configured write is gone; config setup is now the re-runnable `apply-config`.
- **The `append-attempt` subcommand is removed** — `advance` now records the retry context to `attempts.md` itself (see Changed), so there is no separate step to call (or forget).
- **The `claude-cli`/`codex-cli` normalizer names are removed** — replaced by a single LLM-agnostic **`default`** (see Changed). A config using an old name is now rejected by the schema enum.

### Added
- **`driver apply-config --config <path> --values-file <path>`** — the sanctioned config-write path behind `--update-config`. Deep-merges a partial `{driver?, llm?, runners?}` values file into `config.json` (only the named leaves change; a role's whole `runners` array replaces wholesale), validates the merged result (placeholders/invalid runners → nothing written), writes atomically, and seeds the config from the template first if absent. Unlike the removed `set-runners`, it is **re-runnable** — config only ever changes here, kept conservative. Safety: it never deletes the config even when `--values-file` points at the config itself, and a failed apply on an absent config leaves nothing on disk (seed happens only on a valid merge).
- **`--update-config [<plan>]` entry mode + `states/update_config.md`** — a conversational, host-session step (like the planner) that recommends the per-role **runners** (execution mode + model), the **`llm.*`** instructions, and the **`driver`** gate keys, gets the user's approval (required even under `--auto-run`), and calls `apply-config`. A plan path is **optional** (it sharpens the recommendations); omit it to reconfigure from the repo + current config. `--plan`/`--request` **auto-run** it (with their plan) when the config is incomplete; run it standalone to reconfigure.
- **`bootstrap-config` reports `config_complete`** — true when `config.json` is ready to run (runners configured **and** no placeholders), so the SKILL knows whether the config gate needs to run. Reported on both the `created` and `exists` paths.
- **`main-session`/`subagent` persona injection** — `run-stage` now prepends a firm role-switch preamble to the assembled system prompt for handoff modes ("act SOLELY as the dev-pipeline `<role>` … disregard any prior role/context; **do ONLY the work THIS role's instructions define, then STOP — do not take on the other pipeline stages**"; the role instructions win for that role's own work, so e.g. the implementor's build-check is untouched), so prior-role/context bleed cannot weaken the role and a `main-session` implementor does not run the project's test suite itself. `SKILL.md §Role Execution` adds the matching discipline: freshly Read the `system_file` at each role start, and re-anchor as the orchestrator after the role. Bash runners are unaffected (fresh subprocess + hard sandbox).
- **Reviewer-independence rule in `dp-reviewer.md`** (drafted for 5.4.1, shipped here) — "You did not write this code — review it as an independent auditor; judge only the diff+contract from disk, do not rely on prior context or memory of how it was produced." Assembled into the reviewer prompt for **every** mode. **Honest limit:** a same-session reviewer retains latent memory after compaction, so this is best-effort independence, not equivalent to a fresh subagent — ordering stays bash/subagent > main-session.

### Changed
- **`config.example.json` ships `runners` as the `unconfigured` sentinel** (was concrete claude bash defaults). The test suite and the real-LLM e2e harness now define their concrete runners inline instead of reading them from the template.
- **`migrate-config` resets runners to `unconfigured`** (was: replace with the template's bash defaults) — a legacy config is converted to the setup state, then reconfigured via `--update-config`. It also **drops removed `driver` keys** (e.g. the pre-6.0.0 `allow_unattended_header_merge`), which `apply-config`'s deep-merge cannot delete — so a 5.x config carrying one stays repairable.
- **`advance` records the retry context to `attempts.md` itself** — when it routes a failure back to a retry (test→implementation, review→implementation/test_implementation, red_test not-confirmed→re-author) it writes the failure log / blocking findings / vacuous-tests note directly, using the result it already loaded. The old two-step "advance then `append-attempt`" (which a `main-session` orchestrator could forget) is gone; `states/test.md`/`review.md`/`red_test.md` drop the manual step.
- **Normalizer `default` replaces `claude-cli`/`codex-cli`** — the two were identical (both strip a markdown fence + extract the outermost JSON), so they collapse into one LLM-agnostic `default`, which is now the default for both bash and handoff json roles (`passthrough` remains the strict opt-in). A `normalizer` on a **file** role (implementor/test_implementor, which produce a git delta, not JSON) is now rejected, and a removed/unknown normalizer in a config is **named with a hint** (not the generic oneOf error). A pre-6.0.0 name frozen in a run's `config.snapshot.json` still normalizes **leniently** at runtime (any non-`passthrough` value ⇒ tolerant), so an in-flight run resumed after upgrading does not silently start rejecting fenced output.
- **The 5.4.0 absolute ban on a `main-session` reviewer alongside a `main-session` implementor is relaxed to a preference** (drafted for 5.4.1). It over-restricted the exact host it targeted: one with *neither* an LLM CLI *nor* a subagent tool forces every role to `main-session`. Now: prefer `subagent`/`bash` for the reviewer; a `main-session` reviewer is acceptable **only as a last resort**, as a best-effort gate (compact first, rely on the persona preamble + the reviewer's independence rule, warn the user).
- **Global Rule 10's config-write exception** now names the `--update-config`/`apply-config` flow (was `set-runners`). `SKILL.md` gains the `--update-config` entry mode and config gate, drops the header trust gate (old Step 7) and the runner-setup dialog (old Step 5, subsumed by `--update-config`); `states/planning.md` writes a spec-only plan; `dp-planner.md` no longer authors a config header. `AGENTS.md`/`README.md`/`install.sh` updated to match.
- **Default iteration budgets raised**: `max_test_iteration` 3→5 (retries after a test failure) and `max_test_implementation_iteration` 2→3 (test re-authoring when RED is not confirmed) — 3 total attempts was tight for a retry loop meant to catch fixable implementor/test-author mistakes, not just structural ones. `max_review_iteration` stays 3.

## [5.4.0] - 2026-07-09

`config.runners` — previously the one setting nobody ever inferred or confirmed — is now configured through a one-time interactive dialog right after the config is first bootstrapped, instead of silently copying the template's concrete claude commands.

### Added
- **`driver bootstrap-config` seeds `runners` as `"unconfigured"`** — each role gets `[{"type": "unconfigured"}]` instead of the template's concrete bash commands. `config.example.json` itself is untouched (still the known-good bash defaults `migrate-config`, the real-LLM e2e harness, and the test suite rely on); only the newly written `.dev-pipeline/dev-pipeline.config.json` differs.
- **`driver set-runners --config <path> --runners-file <path>`** — the one-time write of the user-confirmed `runners` into a still-unconfigured config: validates the given runners (schema shape + actionable business-rule messages — bash-needs-command, no-command-on-subagent/main-session, homogeneous array, and named errors for unknown/legacy/`unconfigured` types so the SKILL's repair loop can act on them), then atomically replaces `runners` and deletes the scratch file on success (left in place on failure, for the retry). Refuses to run once runners are already configured — edit the config file directly after that.
- **`save_json` is now atomic** (temp file + `fsync` + `os.replace`) — a crash mid-write can no longer truncate `config.json` or `state.json`.
- **`validate-config` detects "not configured yet"** — a config with any `unconfigured` role fails with an actionable message (before the generic schema error), pointing at the interactive setup or `set-runners` directly.
- **`SKILL.md` Step 5: interactive runner-setup dialog** — runs whenever `bootstrap-config` reports `runners_configured: false` (a fresh bootstrap **or** a first run that was interrupted before setup finished — the setup is resumable, not deadlocked), for **both** `--request` and `--plan`, even under `--auto-run`. Detects available CLIs (`command -v claude`/`codex`), proposes a runner per role with reasoning in a single batched message (mirroring the planner's Step 2 confirmation pattern) — bash with a scoped tool envelope when a CLI is available (reviewer defaults to read-only, the only mode with a **hard** sandbox); `subagent`/`main-session` otherwise, with the no-hard-sandbox trade-off stated plainly for the reviewer, and never a `main-session` reviewer when the implementor is also `main-session` (self-review). A bounded (3-attempt) repair loop handles `set-runners` validation failures. This is the SKILL's one sanctioned exception to Global Rule 10 ("never modify the user's config yourself"), scoped to a config whose runners are still unconfigured. `bootstrap-config` reports `runners_configured` on both the `created` and `exists` paths; `migrate-config` refuses a still-unconfigured config (it converts a legacy config, not the setup path).

### Changed
- `bootstrap-config`'s `required_fields`/`next_action` output no longer implies runners are ready; it points at the new setup step first.

## [5.3.0] - 2026-07-09

Role runners gain two **host execution modes** alongside `bash`, so a role can run in the host session instead of shelling out to a CLI — chosen per role, kept LLM-free (no host agent-definition files).

### Added
- **`config.runners.<role>` execution modes** — besides `{type:"bash", command, …}`, a runner may now be `{type:"main-session", normalizer?}` (the host LLM performs the role itself, after compacting the conversation — works on any host) or `{type:"subagent", model?, normalizer?}` (the host spawns a subagent with the driver-assembled prompt injected — no `.claude/agents` file needed, so it stays provider-neutral; `model` is selectable). A role's runner array must be homogeneous (cross-mode fallback is a future feature).
- **`driver run-stage` handoff** — for a `main-session`/`subagent` runner the driver assembles + persists the prompt (as always) but cannot execute it (a subprocess can't call the host's Task tool), so it emits `{mode, system_file, user_file, output_file, model?, compact_first?}` and the SKILL executes it per the new **`SKILL.md §Role Execution`** section (dispatch a subagent, or compact-then-run in the main session; STOP if a subagent runner lands on a host with no Task tool). The `Task` tool is re-added to the SKILL's `allowed-tools` (ignored by hosts that lack it).
- **`driver finalize-stage --run --role [--stage-input]`** — normalizes (strips fences), schema-validates, and persists the canonical JSON for a result the SKILL got from a main-session/subagent runner — the exact post-processing a bash JSON role gets inside `run-stage` (shared `_finalize_json`), so results validate identically regardless of who produced them. File roles are a no-op (their result is the git delta).
- **`validate-config` rules** — accepts the new types; rejects a bash runner without `command`, a main-session/subagent runner *with* one, and a heterogeneous runner array — each with a precise message. The pre-3.0.0 `claude-subagent` type is still rejected (not confused with the new `subagent`, which carries `model`, not `agent`).

### Changed
- `run-stage`'s no-command guard now allows main-session/subagent runners (they have no command by design) while still rejecting an unsupported/legacy type. The bash JSON validation path was factored into `_finalize_json` and shared with `finalize-stage`.

### Security
- `subagent`/`main-session` runners have **no hard tool envelope** (LLM-free means no host agent-definition files); the executor runs with the host's tools, contained only by the role prose (role prompts were reworded to be tool-envelope-agnostic — e.g. "do not run anything even if a Bash tool is available"). A `subagent`/`main-session` **reviewer/tester** reviews untrusted code with write access — for a read-only role prefer a `bash` runner with a scoped `--allowedTools`. Also: don't run `main-session` for the reviewer when the implementor is also `main-session` (the gate becomes self-review). The driver now **stamps the review-result `source`** with the true execution mode (`bash-runner` / `host-subagent` / `main-session`) instead of the role self-reporting a fixed `bash-runner`, so an audit reflects how a review was actually run (schema enum extended). `bash` remains the default and the portable/sandboxable option; the new modes are opt-in and host-coupled. Documented in `AGENTS.md`, `README.md`, and `SKILL.md §Role Execution`.

## [5.2.0] - 2026-07-07

Stronger conversational planner: plans are contract-detailed but implementation-agnostic, and the required config-header values are decided **with the user**.

### Added
- **`dp-planner.md` guidance** — new Global Rules ("right-size to one increment", "specify WHAT, delegate HOW / every added detail must be a testable AC or a real constraint"); Step 1 now captures **concrete reuse targets** (`file:symbol`) and the new-file directory, and derives build/install/test commands **by reading the project's build files** (choosing a test command that runs the new tests *with* the existing suite, so regressions aren't missed); Interface calls for data shapes/error modes; a new optional **`## File Layout`** section (kept consistent with `test_paths`); `## Constraints / Notes` carries explicit `Reuse:` pointers; Acceptance-Criteria guidance now requires one-behavior, concrete, deterministic criteria including edge/error cases.

### Changed
- **Required config-header values are confirmed with the user during planning.** The planner presents the derived `tester.*` commands and (TDD) `test_implementor.framework_instruction` + `test_paths` with their evidence and has the user confirm/correct them instead of silently guessing. In the `--request` flow this confirmation **is** the human consent the executable/gate keys require, so `states/planning.md` now sets `header_approved = true` from it — the confirmed values merge into the run snapshot **even under `--auto-run`** (a hand-written `--plan` still runs the planner-less path and stays gated by SKILL Step 0). `config.json` is never overwritten — the header always merges into the per-run `config.snapshot.json`. The batched confirmation covers **all** executable/gate keys (`tester.*`, `test_paths`, `review_block_severity`, `tdd_mode`), and `header_approved` is set only when that confirmation actually happened. Removed the old `--auto-run` + placeholder-config planning stop — the planner now confirms and fills those values instead.

## [5.1.0] - 2026-07-06

Default runners are now **all `claude`, pinned to the `sonnet` model**; the shipped default reviewer is claude (codex is no longer the default). Role/orchestrator prose is kept LLM-neutral.

### Changed
- **`config.example.json` (bootstrap template)** — every default `claude` runner (`implementor`, `test_implementor`, `tester`, `reviewer`) now passes `--model sonnet`, pinning the model instead of relying on the CLI default. The **codex reviewer runner was removed** from the default `reviewer` array, which now ships a single claude reviewer. Codex remains fully supported as an opt-in runner (the `codex-cli` normalizer, `codex exec -s read-only`, and `llm.reviewer.scope` are unchanged) — add it back to `config.runners.reviewer` to use it.
- **LLM-neutral prose** — the `done` commit `Co-Authored-By` trailer is no longer hardcoded to `Claude`; it names the orchestrator model actually running the skill. Removed the vestigial `model:`/`tools:` frontmatter from the `dp-*.md` role prompts (stripped at assembly; per the "no LLM name in prose" rule). Generalized "codex then claude"/"codex fallback" wording in `states/*.md`, `SKILL.md`, `dp-reviewer.md`, `README.md`, `install.sh`, and `driver.py --help` to reference `config.runners.reviewer` order instead of naming the default LLMs. `AGENTS.md` security/architecture notes updated to reflect the claude default reviewer. The `done` state's "Update CLAUDE.md" step now targets the project's agent memory doc generically (`AGENTS.md`, the open standard; `CLAUDE.md` is just one host's variant/symlink).

### Fixed
- **Review diff omitted brand-new files** — `states/review.md` built the reviewer's `changes_diff` with `git diff HEAD -- <manifest paths>`, which silently drops **untracked** files. For a change consisting of newly-created files (e.g. the first file on a branch), the reviewer received an empty/partial diff and the `dp-reviewer.md` empty-diff guard could raise a spurious blocking finding — observed causing an extra review→implementation round-trip in an end-to-end run. The diff step now marks manifest files intent-to-add (`git add -N`) before diffing and resets afterward, so new files appear as `new file` hunks while the working tree is left unchanged. The reviewer's empty-diff guard is unchanged (a genuinely empty diff still means "nothing to review").
- **Runner timeout orphaned the LLM CLI** — `run-stage` ran each runner via `subprocess.run(shell=True, timeout=…)`, which on timeout SIGKILLs only the direct child shell, leaving the LLM CLI it spawned orphaned (reparented to PID 1) and still running (wasting compute and able to write its output file late). `_run_one` now starts the runner in its own session (`start_new_session=True`) and SIGKILLs the whole **process group** on timeout — or when the driver itself is interrupted. (Limits: a grandchild that calls `setsid()` itself escapes the group, and a `SIGKILL` of the driver leaves nothing to clean up.)

### Migration
- No action needed for existing installs — their `.dev-pipeline/dev-pipeline.config.json` is unchanged. New runs bootstrapped after upgrading get the sonnet-pinned, claude-only reviewer default. To keep a codex reviewer, add its runner to `config.runners.reviewer`. To use a different claude model, change `--model` in the runner commands.

## [5.0.0] - 2026-07-05

Conversational **planner** front-end; `plan.md` becomes the single contract (config header + spec body); `spec.md` / the `spec_author` role are removed.

### Added
- **`dp-planner.md` + `states/planning.md`** — a conversational planner that runs in the **host session** (not a headless runner). `/dev-pipeline --request "<goal>"` refines the goal, explores the repo read-only, asks the user when ambiguous, decides TDD/no-TDD, and writes one `plan.md`, then runs the pipeline. `--auto-run` skips the post-plan approval gate (planning-phase questions still happen).
- **`plan.md` config header** — a leading fenced `dev-pipeline-config` JSON block. `init` parses it and merges a **trust-tiered whitelist** into the run's `config.snapshot.json` (never `config.json`): prose keys (`design_instruction`, `focus`, `framework_instruction`, `reviewer.scope`) always; executable/gate keys (`tester.*` commands, `test_paths`, `review_block_severity`, `tdd_mode`) only with human approval (`init --header-approved`, set by the SKILL on approval) or the durable `driver.allow_unattended_header_merge`. `runners` are **never** merged. Parsing is **fail-closed**: a malformed header is a hard error, never a silent fallback.
- **`driver validate-config --plan <path>`** — validate the config exactly as `init` will (header merged, plan body sections checked).
- **`driver.allow_unattended_header_merge`** (optional config bool) — opt into unattended executable/gate header merges.

### Changed (breaking)
- **Removed `spec.md` and the `spec_author` role.** The header-stripped plan **body is the contract**, written by `init` to `<run_dir>/contract.md` and read by the test author, implementor, and reviewer. `init` validates the required body sections deterministically (`## Requirements`, `## Acceptance Criteria`, and `## Interface` under TDD) with non-empty checks — replacing the LLM `INSUFFICIENT` refusal. Section validation runs **before** any run directory is created.
- **Removed the `--tdd` / `--no-tdd` flags.** `tdd_mode` is sourced solely from `driver.tdd_mode` (which a plan header may set) and frozen into `state.tdd_mode` at init.
- **State key `spec_path` → `contract_path`** (with a `spec_path` fallback so an in-flight pre-5.0.0 run stays inspectable). The `plan_path` echo to downstream roles was dropped — roles read only `contract.md`.
- **`runners.spec_author` removed** from the schema/template; a config still carrying it is rejected with a `migrate-config` hint. The run-stage `"named"` category and `INSUFFICIENT` machinery were removed.
- `install.sh` now installs `dp-planner.md` and `states/planning.md` (9 state files) and no longer `dp-spec-author.md`.

### Migration
- Re-run `bash install.sh <project>`. Old configs carrying `runners.spec_author` fail validation → run `driver migrate-config --config <path>` (drops it). A pre-5.0.0 run in flight should be finished with the driver version that started it. Hand-written plans now need the section headings above; add a `dev-pipeline-config` header (or keep the instructions in `config.json`).

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
