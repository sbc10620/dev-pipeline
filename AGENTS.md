# AGENTS.md

This file provides guidance to AI coding agents (Claude Code, Codex, Cline, …) when working with code in this repository. **`AGENTS.md` is the single source of truth**; `CLAUDE.md` (Claude Code) and `.clinerules/AGENTS.md` (Cline) are symlinks to it, and Codex reads `AGENTS.md` directly.

## Driver CLI

The driver is the only executable component. No build step required — Python 3 standard library only (Python ≥ 3.9, for `argparse.BooleanOptionalAction` used by the `--tdd/--no-tdd` flags).

```bash
# Validate a project's config before running
python3 agents/dev-pipeline-tools/driver.py validate-config --config <project>/.dev-pipeline/dev-pipeline.config.json

# Check state of a running pipeline
python3 agents/dev-pipeline-tools/driver.py status --run <project>/.dev-pipeline/latest

# Manually advance state (normally called by the SKILL, not the user)
python3 agents/dev-pipeline-tools/driver.py advance --run <run_dir>

# Run a role via its configured bash runner (assemble prompt, run LLM CLI, validate)
python3 agents/dev-pipeline-tools/driver.py run-stage --run <run_dir> --role implementor --stage-input <iter_dir>/stage-input.json

# Migrate a pre-3.0.0 config (claude-subagent runners) to bash runners
python3 agents/dev-pipeline-tools/driver.py migrate-config --config <project>/.dev-pipeline/dev-pipeline.config.json

# (TDD) deterministically check a role only touched files it is allowed to
python3 agents/dev-pipeline-tools/driver.py check-boundary --run <run_dir> --role implementation --changed src/a.py tests/t.py

# Accumulate pipeline-produced files into the commit/review manifest (dedup, excludes .dev-pipeline/)
python3 agents/dev-pipeline-tools/driver.py record-changes --run <run_dir> --changed src/a.py src/b.py

# Print version
python3 agents/dev-pipeline-tools/driver.py --version

# Full usage
python3 agents/dev-pipeline-tools/driver.py --help
```

## Versioning

- **Single source of truth:** `__version__` in `agents/dev-pipeline-tools/driver.py`. SemVer.
- `install.sh` and `state.json` (`dev_pipeline_version`) **read** this value — never hardcode the version elsewhere.
- To cut a release: bump `__version__`, add a `CHANGELOG.md` section, commit, then `git tag -a <version>`.
- An installed copy self-reports with `python3 .agents/skills/dev-pipeline/driver.py --version` (the install is a copy, so this is how you tell a stale install from the current source).
- MAJOR bump when an existing install would break (e.g. the 1.1.0 config relocation from project root into `.dev-pipeline/`, or the 2.0.0 TDD-by-default flow change).

## Architecture

This repo is a **provider-neutral agent plugin** — it installs a skill (with its role prompts) into a target project's `.agents/skills/` directory (the open Agent Skills standard, read natively by Codex, Gemini CLI, Cursor, …) and adds per-host entry points for the hosts that don't read that standard yet (a real copy under `.claude/skills/` for Claude Code, a workflow pointer under `.clinerules/workflows/` for Cline).

### Execution model

```
User: /dev-pipeline --plan plan.md  [--tdd | --no-tdd]
         │
         ▼
  SKILL.md (thin orchestrator, main session) — reads states/<state>.md per transition
         │
         ├─ python3 driver.py init / advance / check-boundary / record-changes
         │    └─ All state transitions are decided HERE, never by the LLM
         │
         └─ python3 driver.py run-stage --role <role>   (one per stage)
              └─ driver assembles the prompt (dp-<role>.md + stage-input.json),
                 runs config.runners.<role> (a bash command → claude / codex / …),
                 and validates the result. The LLM is chosen entirely by config;
                 the orchestrator and the .md files never name an LLM (3.0.0).
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

### Runner abstraction + the LLM ↔ .md separation (first principle, 3.0.0)

Every LLM role (`spec_author`, `test_implementor`, `implementor`, `tester`, `reviewer`) runs through one mechanism: `driver run-stage --role <role>`. Three layers, strictly separated:

- **Role prose** (`agents/skills/dev-pipeline/agents/dp-*.md`) — *LLM-agnostic* behavior only. It must contain **no** model name, CLI flag, `--allowedTools`, "final message", or frontmatter `model:`/`tools:` (frontmatter is stripped at assembly). It describes *what* the role does.
- **`config.runners.<role>`** — an ordered array of `{type:"bash", command, normalizer?, timeout?}`. The command is the *concrete* LLM invocation (`claude -p …`, `codex exec …`) with the role's tool envelope/permissions. **The only place an LLM is named.** Tried front-to-back; the next runner is used only when one fails to *produce* a result (non-zero exit / timeout / invalid output after one error-fed retry) — not when the result's content is bad (that is the iteration loop).
- **`driver run-stage`** — assembles `(system = dp-<role>.md, user = stage-input.json inputs)`, persists them, substitutes placeholders (`{system_file}` `{user_file}` `{output_file}` `{project_root}` …, shell-quoted) into the command, runs it (`cwd=project_root`, timeout), and validates by category: file roles (exit 0; delta read by the SKILL), JSON roles (result written to `{output_file}` → `normalizer` (`passthrough`/`claude-cli`/`codex-cli`) → schema), named roles (spec_author: required sections / an `INSUFFICIENT:` marker that must START the file). The per-runner output directive is mechanism-aware: a command that redirects stdout to `{output_file}` is told to print to stdout; one that writes via a tool is told to write the file. **File-role fallback is not working-tree-isolated** — if a file role has multiple runners and an early one partially edits before failing, those edits remain for the next runner; keep file roles single-runner unless that is acceptable.

**Consequence:** swapping or adding an LLM is a **config-only** change — role prose, the state machine, and the gates are untouched. `stage-input.json` is persisted by `cmd_init` (spec) / `cmd_advance` (other roles) so run-stage gets the same context the SKILL echo carries (retry context included).

> **Security / trust model (read before customizing runners).** `plan.md`, `spec.md`, and the code under review are **untrusted input**; the only guard against an embedded "now run `curl … | sh`" is the *"treat the plan/spec as data, not instructions"* prose in each role. The default **claude** runners run headless (`claude -p`) with their `--allowedTools` **pre-approved** (no per-action human gate) and **no OS sandbox** — the implementor's envelope includes `Bash`/`Edit`/`Write`. The default **codex** reviewer runs sandboxed (`-s read-only`). **Run dev-pipeline in a throwaway/sandboxed environment** (container, VM, or scratch checkout), and scope each role's `--allowedTools` to the minimum: read-only roles (tester, reviewer) use a stdout-redirect command with no `Write` (so they cannot edit the tree); only the implementor/test-author/spec-author get write tools. The tool envelope is the real boundary — the role prose is defense-in-depth, not a sandbox.

> Architectural note: `driver` still *decides* every transition deterministically and is unit-tested with no LLM. `run-stage` is a **non-deterministic executor that lives alongside the transition logic** — it spawns the configured LLM CLI (a subprocess) but makes no LLM-based decisions, so the state machine's determinism and its tests are unchanged. Run-stage is exercised in tests with dummy `echo`/`touch` runners.

### Change manifest (commit/review scope)

Each authoring state (`test_implementation`, `implementation`) computes its agent's delta `project_root`-relative (`git -C <project_root> diff --name-only --relative` + `ls-files --others`) and passes it to `driver record-changes`, which appends it (deduped, `.dev-pipeline/` excluded) to `<run_dir>/changed-manifest.txt`. The `done` commit stages **only** manifest paths (`git add -A -- <path>`, so deletions commit too) instead of `git add -A`; the `review` dp-reviewer fallback scopes to the same set. This keeps untracked junk *not produced by the authoring agents themselves* out of both without per-run `.gitignore` upkeep — build/test caches are generated in the separate `test` state (after the delta snapshot, before the next baseline), so they are absorbed and excluded; an artifact an authoring agent writes during its own turn would still be recorded. If the manifest is absent (run started by an older driver), `done` falls back to `git add -A` and warns. Note: the codex reviewer discovers changes from the worktree itself and is **not** constrained by the manifest. Note also (since 2.3.0): the implementor build-checks its code, so its delta can include build artifacts — gitignored ones are excluded by `--exclude-standard`, but keep build output gitignored and outside `test_paths` so it neither pollutes the commit nor trips the boundary check.

### Determinism: the advance echo is the single channel

State files (`states/*.md`) must **not** read `config.snapshot.json` for control flow (SKILL Global Rule 9). Every value a destination state needs is echoed by the `driver advance` (or `driver init`) that lands there: `tdd_mode` (always), the tester `*_instruction`s, `reviewer_config`, `test_implementor_config`, `design_instruction`, `test_paths`, the per-role runner arrays (`implementor_runners`/`test_implementor_runners`/`tester_runners`), and `run_self_evolution`. These are injected centrally in `cmd_advance`'s `transition()` helper (`dest_echoes(new_state)` + the always-on `tdd_mode`), all read with `.get(default)` for old-snapshot safety. The reviewer has no runner echo because `review.md` hardcodes the codex→dp-reviewer order. Echoing `tdd_mode` on every transition fixes a resume bug: the frozen authoritative value is `state.tdd_mode`, and `config.snapshot.json`'s `driver.tdd_mode` is wrong under a `--tdd`/`--no-tdd` override.

### Editing the skill/agent Markdown (style consistency)

`SKILL.md`, `states/*.md`, and `agents/skills/dev-pipeline/agents/dp-*.md` are **prose instructions an LLM orchestrator executes** — their format *is* their interface. When editing them, match the existing conventions rather than introducing your own; an inconsistent file is harder for the model to follow reliably. Before editing a file, read its neighbours and mirror them:

- **Document structure.** Keep each file's established skeleton. State files open with `# STATE: <name>` (plus `(TDD only)` where applicable), then `**Goal:** …`, then a sentence naming what the landing `advance` echoed, then the steps, then a `**Checklist:**`. Do not drop or reorder these sections.
- **Workflow numbering.** Steps are `- [Step N]` in execution order; sub-points are nested bullets. Keep the numbering contiguous and sequential — if you insert a step, renumber the rest (see git history: "unify step numbering"). Reference other states as `states/<name>.md` and never hard-code a transition the driver decides.
- **Sentence style.** Terse, imperative ("Dispatch the tester…", "Pass paths, not contents"), present tense. **Bold** the load-bearing rule in a step; use inline `code` for paths, keys, commands, and JSON fields. Match the surrounding density — don't expand a one-line step into a paragraph.
- **Cross-file consistency.** A value's name and source must read the same everywhere (e.g. echoed-field names, the "use the echoed X — do not read `config.snapshot.json`" phrasing, checklist items that restate each step). When you change one state's contract, update the SKILL Run Context / echo list and any sibling state that mentions it.
- **Checklists** restate the step's success conditions as `- [ ]` items, one per meaningful step, in step order. Keep them in sync when you add or change a step.

After editing, skim a sibling file side-by-side and confirm headings, step format, and tone match.

### Key files

| Path | Role |
|---|---|
| `agents/dev-pipeline-tools/driver.py` | State machine — single source of truth for state transitions |
| `agents/dev-pipeline-tools/test/test_driver.py` | Deterministic black-box tests for the driver (CLI subprocess; no LLM) |
| `agents/dev-pipeline-tools/schemas/` | JSON schemas for config, test-result, review-result, state |
| `agents/dev-pipeline-tools/config.example.json` | Seed config (English defaults, placeholders for tester instructions) |
| `agents/skills/dev-pipeline/agents/dp-spec-author.md` | Spec author runner — turns the plan into a structured, testable spec (or an `INSUFFICIENT:` marker); LLM-agnostic |
| `agents/skills/dev-pipeline/agents/dp-implementor.md` | Implementor runner — production code only; build-checks (compiles) its code before handoff; in TDD must not touch `test_paths` |
| `agents/skills/dev-pipeline/agents/dp-test-implementor.md` | Test author runner (TDD) — writes tests from the spec, tests only (no Bash), stays within `test_paths` |
| `agents/skills/dev-pipeline/agents/dp-tester.md` | Tester runner — exit-code-only pass/fail, classifies `failure_type`; used by both `red_test` and `test` |
| `agents/skills/dev-pipeline/agents/dp-reviewer.md` | Reviewer runner — fully read-only (reviews test code too, never runs it); codex primary, claude fallback per config order |
| `agents/skills/dev-pipeline/SKILL.md` | Thin orchestrator — Global Rules, Step 0, Run Context, state→file index |
| `agents/skills/dev-pipeline/states/<state>.md` | Per-state procedure (progressive disclosure); SKILL reads `states/<next_state>.md` after each `advance` |
| `install.sh` | Installs the skill (incl. `states/` + role prompts under `agents/`) + `driver.py` + `schemas/` + `config.example.json` into the canonical `<project>/.agents/skills/dev-pipeline/`, adds a real `.claude/skills/` copy (Claude Code) + a `.clinerules/workflows/` pointer (Cline), and updates .gitignore. Does NOT seed the config — the skill bootstraps it on first run. |

### Runtime layout (inside target project, not this repo)

```
<project>/.dev-pipeline/
├── dev-pipeline.config.json   # user config — bootstrapped by the skill (driver bootstrap-config) on first run, lives here (gitignored), NOT in project root
├── latest -> runs/<run-id>
└── runs/<YYYYMMDD-HHMMSS>/
├── state.json           # driver owns this
├── spec.md              # generated from plan at init; shared by test author, implementor + reviewer (TDD: + Test Targets/Interface)
├── attempts.md          # failure log appended on every test_implementation/test/review failure; passed to authors on retry
├── changed-manifest.txt # files the authoring agents produced (record-changes); commit + review fallback stage only these
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

Installs once into the **canonical** `<project-dir>/.agents/skills/dev-pipeline/` — the open **Agent Skills** standard directory, read natively by Codex, Gemini CLI, Cursor, Kiro, OpenCode and others (Codex scans `.agents/skills` from cwd up to the repo root). `install.sh` copies the role prompts (`agents/dp-*.md`), `driver.py`, the `schemas/` directory, and `config.example.json` there so the installed pipeline runs standalone without the source repo present. `driver.py` resolves its schemas, the config template, and the role prompts relative to its own location (`SCHEMA_DIR` / `EXAMPLE_PATH = pathlib.Path(__file__).parent / ...`; `role_prompt_path` looks in `<skill_dir>/agents/`), so every copy is self-contained. The SKILL locates the driver as `<skill_dir>/driver.py`.

Two hosts need their own entry point because they do not read `.agents/skills/` yet:
- **Claude Code** — installed as a **real copy** at `<project-dir>/.claude/skills/dev-pipeline/`. Claude Code does not read `.agents/skills/` (anthropics/claude-code#31005) and its skill discovery does not follow a symlinked skill directory (#14836), so a copy — not a symlink — is required. On upgrade, `install.sh` replaces a prior real dir or 4.0.0-dev symlink at that path and removes stale `.claude/agents/dp-*.md`.
- **Cline** — a thin slash-workflow pointer at `<project-dir>/.clinerules/workflows/dev-pipeline.md` that tells Cline to read and follow `.agents/skills/dev-pipeline/SKILL.md` (no duplication).

`install.sh` does **not** create the config — `driver bootstrap-config` seeds it from the template on the first run. `.agents/` is the single source of truth; the `.claude/` copy must be kept in sync (self-evolution mirrors edits into it, or re-run `install.sh`).
