# dev-pipeline

Automated **test-driven** development pipeline for Claude Code: author tests from the spec, prove they fail (RED), write code, prove they pass (GREEN), then review.

Accepts a `plan.md` written by any LLM (Claude, Codex, etc.) and drives the full development cycle using per-stage LLM runners (claude, codex, …; chosen in config), with deterministic state transitions handled by a Python driver script.

---

## How it works

```
plan.md
   │
   ▼
 [init]  →  validate config, generate spec.md (incl. Test Targets / Interface)
   │
   ▼
 [test_implementation]  →  test author writes tests from the spec        ┐ TDD
   │                                                                      │ (default;
   ▼                                                                      │  skipped
 [red_test]  →  tester proves the tests FAIL (no code yet)                ┘  with --no-tdd)
   │ red confirmed
   ▼
 [implementation]  →  implementor agent writes code
   │
   ▼
 [test]  →  tester agent runs build / install / test
   │ pass                    │ fail (code)
   ▼                         ▼
 [review]           [implementation] (retry, with failure context)
   │ approve                 │ fail
   ▼                         ▼
 [done]                   [failed]
```

- **TDD is on by default.** Run with `--no-tdd` (or set `driver.tdd_mode: false`) to skip `test_implementation`/`red_test` and use the legacy `implement → test → review` flow.
- RED not confirmed (authored tests pass with no code) → re-author tests (up to `max_test_implementation_iteration` times)
- Test failure → retry implementation (up to `max_test_iteration` times)
- Review failure → retry implementation, or — if the blocking finding is about a test file — re-author tests (up to `max_review_iteration` times)
- A role boundary keeps the test author and implementor in lane: the implementor never edits test files, the test author never writes production code (enforced by `driver check-boundary`)
- Exhausted iterations or environment error → `failed` state with user report
- State transitions are always decided by the driver script, never by the LLM

---

## Installation

```bash
bash /path/to/dev-pipeline/install.sh /path/to/your/project
```

This copies the skill (incl. `states/` and the role prompts under `agents/`), `driver.py`, schemas, and the config template into the canonical `<project>/.agents/skills/dev-pipeline/` (the open Agent Skills standard, read by Codex/Gemini/Cursor/…), plus a real `.claude/skills/` copy for Claude Code and a `.clinerules/workflows/` pointer for Cline. It does NOT create the config — the skill bootstraps `dev-pipeline.config.json` from the template (via `driver bootstrap-config`) into the gitignored `<project>/.dev-pipeline/` directory on the first run (so it never clutters the project root or gets confused with your own source files). The pipeline runs standalone — the dev-pipeline source repo does not need to be present.

---

## Configuration

Edit `.dev-pipeline/dev-pipeline.config.json` in your project. The three tester instructions are **required** — the tester will never infer or guess commands.

```json
{
  "driver": {
    "max_test_iteration": 3,
    "max_review_iteration": 3,
    "max_test_implementation_iteration": 2,
    "tdd_mode": true,
    "run_self_evolution": false,
    "review_block_severity": ["critical", "high"]
  },
  "llm": {
    "implementor": {
      "design_instruction": "Prefer reusing existing code patterns..."
    },
    "test_implementor": {
      "focus": "One meaningful test per acceptance criterion...",
      "framework_instruction": "pytest under tests/, one test per acceptance criterion",
      "test_paths": ["tests/**"]
    },
    "tester": {
      "build_instruction":   "npm run build",
      "install_instruction": "npm ci",
      "test_instruction":    "npm test"
    },
    "reviewer": {
      "focus": "Adversarially review for correctness...",
      "scope": "working-tree"
    }
  },
  "runners": {
    "spec_author":      [{ "type": "bash", "command": "cat {user_file} | claude -p --append-system-prompt-file {system_file} --allowedTools Read Write" }],
    "implementor":      [{ "type": "bash", "command": "cat {user_file} | claude -p --append-system-prompt-file {system_file} --allowedTools Read Edit Write Bash" }],
    "test_implementor": [{ "type": "bash", "command": "cat {user_file} | claude -p --append-system-prompt-file {system_file} --allowedTools Read Edit Write" }],
    "tester":           [{ "type": "bash", "command": "cat {user_file} | claude -p --append-system-prompt-file {system_file} --allowedTools Read Bash > {output_file}", "normalizer": "claude-cli" }],
    "reviewer":    [
      { "type": "bash", "command": "codex exec -s read-only \"$(cat {system_file}; printf '\\n\\n'; cat {user_file})\" > {output_file}", "normalizer": "codex-cli" },
      { "type": "bash", "command": "cat {user_file} | claude -p --append-system-prompt-file {system_file} --allowedTools Read Grep Glob > {output_file}", "normalizer": "claude-cli" }
    ]
  }
}
```

**Runners (3.0.0).** Each role runs through `driver run-stage`, which assembles the prompt from the LLM-agnostic `dp-<role>.md` + the stage's inputs and runs `config.runners.<role>` — an ordered array of `{ "type": "bash", "command": …, "normalizer"?: "passthrough|claude-cli|codex-cli" }`. **The command is the only place an LLM is named**; swap/add an LLM by editing config alone. Placeholders the driver substitutes: `{system_file}` `{user_file}` `{output_file}` `{project_root}` `{run_dir}` `{work_dir}`. JSON roles either write `{output_file}` (tool) or print to stdout (when the command redirects `> {output_file}`). `runners.spec_author` is required; `llm.test_implementor` + `runners.test_implementor` are required only under TDD (the default — `--no-tdd` / `tdd_mode:false` to omit).

> **Security:** the default `claude` runners run headless with pre-approved tools and **no sandbox**; `plan.md`/`spec.md`/code are untrusted input. Run dev-pipeline in a sandboxed/throwaway environment and keep each role's `--allowedTools` minimal (read-only roles use a stdout-redirect command with no `Write`). A pre-3.0.0 config is rejected with a hint — run `driver migrate-config --config <path>` to convert.

### Config fields

| Field | Required | Description |
|---|---|---|
| `driver.max_test_iteration` | Yes | Max implementation retries after test failure |
| `driver.max_review_iteration` | Yes | Max implementation retries after review failure |
| `driver.max_test_implementation_iteration` | No | Max test re-authoring when RED is not confirmed (default: 2) |
| `driver.tdd_mode` | No | Author tests first (RED→GREEN). Default `true`. Override per run with `--tdd`/`--no-tdd` |
| `driver.run_self_evolution` | Yes | Update installed agent .md files after done (default: false) |
| `driver.review_block_severity` | No | Severities that block review pass (default: `["critical","high"]`). Null = use verdict gate |
| `llm.tester.build_instruction` | **Yes** | Exact build command. Use `"no build step"` if not needed |
| `llm.tester.install_instruction` | **Yes** | Exact install command. Use `"no install step"` if not needed |
| `llm.tester.test_instruction` | **Yes** | Exact test command. Use `"no test step"` if not needed |
| `llm.test_implementor.framework_instruction` | TDD | Test framework + where/how tests are written |
| `llm.test_implementor.test_paths` | TDD | Globs matching test files only — the role boundary (e.g. `["tests/**"]`) |
| `llm.reviewer.scope` | No | Codex review scope: `working-tree` (default), `branch`, `auto` |

---

## Usage

In Claude Code, with your project open:

```
/dev-pipeline --plan plan.md            # TDD by default
/dev-pipeline --plan plan.md --no-tdd   # legacy implement → test → review
/dev-pipeline --help
```

**Prerequisites:**
- `.dev-pipeline/dev-pipeline.config.json` must be present and valid
- **Commit the installed dev-pipeline files** (the canonical `.agents/skills/dev-pipeline/` tree, the `.claude/skills/dev-pipeline/` copy, and `.clinerules/workflows/dev-pipeline.md`) before running. They are tracked (not gitignored, so self-evolution can manage their history), and the review uses `working-tree` scope — committing them keeps the reviewer focused on your code instead of dev-pipeline's own tooling.
- Start with a **clean working tree** (no unrelated uncommitted changes — they will be included in the review scope)
- **Gitignore your build outputs** (compiled binaries, object files, etc.). The review uses `working-tree` scope, so any untracked artifact produced by the test phase would otherwise be reviewed alongside your real changes.

---

## Roles

Each role is an LLM-agnostic prose file (`agents/skills/dev-pipeline/agents/dp-<role>.md`) run by `driver run-stage` through its `config.runners.<role>` command. The **tool envelope** below is whatever that command's flags grant (e.g. claude `--allowedTools`) — set in config, not in the role file.

| Role | Does | Tool envelope (set in config) |
|---|---|---|
| `dp-spec-author` | Turns the plan into a structured, testable spec (or an `INSUFFICIENT:` marker) | Read, Write (spec only) |
| `dp-test-implementor` | (TDD) Writes tests from the spec — tests only, no production code | Read, Write, Edit (no Bash) |
| `dp-implementor` | Writes + build-checks code from plan + spec; never edits tests under TDD | Read, Edit, Write, Bash |
| `dp-tester` | Runs build/install/test — **no code inference** (used by `red_test` and `test`) | Read, Bash (read-only; no Write) |
| `dp-reviewer` | Adversarial review against the spec; reads the diff, never edits | Read, Grep, Glob (read-only) |

---

## Reviewer: codex primary, claude fallback

`config.runners.reviewer` is an ordered array tried front-to-back. The default ships **codex** (`codex exec -s read-only`) first and **claude** (`claude -p`, read-only tools) as the fallback; the next runner is used only if one fails to produce a valid `review-result.json`. Both review the change diff against the spec's Acceptance Criteria. Customize or reorder by editing the config.

---

## Review gate

By default, findings with `critical` or `high` severity block the review pass.
Configure with `driver.review_block_severity`. Set to `null` to use verdict-based gating instead.

---

## Runtime directory

Created at `<project>/.dev-pipeline/` (gitignored automatically).

```
.dev-pipeline/
├── dev-pipeline.config.json # your config — bootstrapped by the skill on first run (gitignored)
├── latest -> runs/<run-id>
└── runs/<run-id>/
    ├── state.json           # driver state (single source of truth)
    ├── spec.md              # generated from plan — shared by test author, implementor and reviewer
    ├── attempts.md          # accumulated failure history — passed to authors on retry
    ├── config.snapshot.json
    ├── changed-manifest.txt  # files the runners produced (commit/review scope)
    ├── stage-input.json      # spec-author stage input
    └── iterations/<n>/
        ├── red-test-result.json   # TDD: the red_test (RED verification) result
        ├── test-result.json
        ├── review-result.json
        └── <role>-system.txt / <role>-user.txt / <role>-output.json  # run-stage prompt+output (audit)
```

Each `run-id` is a timestamp (`YYYYMMDD-HHMMSS`). Previous runs are preserved for audit/debug.

---

## Driver CLI (advanced)

```bash
python3 agents/dev-pipeline-tools/driver.py --help
python3 agents/dev-pipeline-tools/driver.py --version
python3 agents/dev-pipeline-tools/driver.py validate-config --config .dev-pipeline/dev-pipeline.config.json
python3 agents/dev-pipeline-tools/driver.py status --run .dev-pipeline/latest
python3 agents/dev-pipeline-tools/driver.py migrate-config --config .dev-pipeline/dev-pipeline.config.json
```

---

## Testing

Deterministic tests for the state machine live in `agents/dev-pipeline-tools/test/`.
They drive `driver.py` as a CLI subprocess — exactly as the SKILL does — and assert on
state transitions, the review gate, schema validation, and the auxiliary subcommands.

```bash
python3 agents/dev-pipeline-tools/test/test_driver.py
# or
python3 -m unittest discover -s agents/dev-pipeline-tools/test -v
```

Standard library only, no external dependencies. The tests do **not** invoke any LLM
agent or codex — they verify `driver.py`'s deterministic logic in isolation.

---

## Versioning

dev-pipeline follows [Semantic Versioning](https://semver.org/). The version is
defined once, in `driver.py`, and read everywhere else.

```bash
# Source repo
python3 agents/dev-pipeline-tools/driver.py --version

# An installed copy (tells you whether your install is stale vs. the latest source)
python3 .claude/skills/dev-pipeline/driver.py --version
```

Each run also records the version under `dev_pipeline_version` in its `state.json`.
See [CHANGELOG.md](CHANGELOG.md) for release history. Because installs are copies,
re-run `install.sh` to upgrade an existing install to a newer version.

---

## Directory structure

### Source repo
```
dev-pipeline/
├── install.sh
├── README.md
└── agents/
    ├── skills/
    │   └── dev-pipeline/
    │       ├── SKILL.md
    │       ├── states/            ← per-state procedure files (init, red_test, …)
    │       └── agents/            ← LLM-agnostic role prompts
    │           ├── dp-spec-author.md
    │           ├── dp-implementor.md
    │           ├── dp-test-implementor.md
    │           ├── dp-tester.md
    │           └── dp-reviewer.md
    └── dev-pipeline-tools/
        ├── driver.py
        ├── config.example.json
        ├── test/
        │   └── test_driver.py
        └── schemas/
            ├── config.schema.json
            ├── test-result.schema.json
            ├── review-result.schema.json
            └── state.schema.json
```

### After installation in target project
```
<project>/
├── .dev-pipeline/
│   └── dev-pipeline.config.json   ← your config (gitignored, not in project root)
├── .agents/                       ← canonical install (open Agent Skills standard)
│   └── skills/                      read natively by Codex, Gemini CLI, Cursor, Kiro, …
│       └── dev-pipeline/
│           ├── SKILL.md
│           ├── states/             ← per-state procedure files (read on demand)
│           ├── agents/             ← role prompts (dp-spec-author … dp-reviewer)
│           ├── driver.py           ← installed for standalone operation
│           ├── config.example.json ← template for driver bootstrap-config
│           └── schemas/            ← config / test-result / review-result / state
├── .claude/
│   └── skills/
│       └── dev-pipeline/           ← real copy for Claude Code (see note below)
└── .clinerules/
    └── workflows/
        └── dev-pipeline.md         ← Cline slash-workflow pointer → .agents/…/SKILL.md
```

> **Why does Claude Code get a copy instead of reading `.agents/skills/`?** Claude
> Code doesn't read the `.agents/skills/` standard yet
> ([anthropics/claude-code#31005](https://github.com/anthropics/claude-code/issues/31005))
> and its skill discovery won't follow a symlinked skill directory
> ([#14836](https://github.com/anthropics/claude-code/issues/14836)), so the
> installer mirrors the canonical tree as real files under `.claude/skills/`.
> Codex and other Agent-Skills hosts need no copy. When Claude Code adds
> `.agents/skills/` support, the copy can go away.

---

## Design notes

- **Deterministic state**: all state transitions go through `driver.py` — the LLM never decides the next state
- **Pluggable runners**: `runners` config is an ordered array of backends; add `bash` runner for other CLIs (e.g., cline)
- **Oscillation prevention**: `attempts.md` accumulates every failed attempt and is passed to the implementor on retry
- **Environment vs code failures**: tester classifies failures; environment failures halt immediately instead of retrying
- **Self-evolution**: when enabled, uses the done-state retrospective to update the installed agent `.md` files and the skill (`SKILL.md`) (source repo not updated)
