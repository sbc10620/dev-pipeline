# dev-pipeline

Automated **test-driven** development pipeline for coding agents: author tests from the contract, prove they fail (RED), write code, prove they pass (GREEN), then review.

Give it a goal — a **conversational planner** writes a single `plan.md` (a testable spec body) — or hand it a `plan.md` you already wrote. Config (runners, tester instructions, gate keys) is set separately by a conversational **`--update-config`** step and stored in `config.json`. It then drives the full development cycle using per-stage LLM runners (claude, codex, …; chosen in config), with deterministic state transitions handled by a Python driver script.

---

## How it works

```
/dev-pipeline --request "<goal>"   (planner writes plan.md)   |   --plan plan.md   |   --update-config [<plan>]   |   --resume [<run_dir>]
   │
   ▼
 [update_config] (--update-config, or auto when config is incomplete)  →  recommend runners + instructions + gate keys, you approve, apply-config writes config.json
   │
   ▼
 [planning] (--request)  →  planner explores read-only, asks you, writes plan.md spec, you approve
   │
   ▼
 [init]  →  validate config + contract, snapshot config, write contract.md
   │
   ▼
 [test_implementation]  →  test author writes tests from the contract   ┐ TDD
   │                                                                     │ (default;
   ▼                                                                     │  skipped when
 [red_test]  →  tester proves the tests FAIL (no code yet)               ┘  tdd_mode=false)
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

- **TDD is on by default.** Set `driver.tdd_mode: false` (in the config, via `--update-config`) to skip `test_implementation`/`red_test` and use the legacy `implement → test → review` flow.
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

The bootstrapped config starts **incomplete** (runners are the `unconfigured` sentinel; tester instructions are placeholders). The `--update-config` step fills it in: the skill recommends a runner (execution mode + model) per role plus the `llm.*` instructions and `driver` gate keys with its reasoning — confirm or correct in one turn, and it writes them via `driver apply-config`. `--plan`/`--request` run this automatically when the config is incomplete; you can also run `/dev-pipeline --update-config` any time to reconfigure (an optional plan path sharpens the recommendations).

---

## Configuration

Config lives in `.dev-pipeline/dev-pipeline.config.json`. Normally you set it through `--update-config` (see Installation), which recommends values and writes them via `driver apply-config` (validated, atomic, re-runnable). The three tester instructions are **required** — the tester will never infer or guess commands. `runners` (shown below fully configured) starts as an `"unconfigured"` sentinel per role. You can also hand-edit this file directly.

```json
{
  "driver": {
    "max_test_iteration": 5,
    "max_review_iteration": 3,
    "max_test_implementation_iteration": 3,
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
    "implementor":      [{ "type": "bash", "command": "cat {user_file} | claude -p --model sonnet --append-system-prompt-file {system_file} --allowedTools Read Edit Write Bash" }],
    "test_implementor": [{ "type": "bash", "command": "cat {user_file} | claude -p --model sonnet --append-system-prompt-file {system_file} --allowedTools Read Edit Write" }],
    "tester":           [{ "type": "bash", "command": "cat {user_file} | claude -p --model sonnet --append-system-prompt-file {system_file} --allowedTools Read Bash > {output_file}", "normalizer": "default" }],
    "reviewer":    [
      { "type": "bash", "command": "cat {user_file} | claude -p --model sonnet --append-system-prompt-file {system_file} --allowedTools Read Grep Glob > {output_file}", "normalizer": "default" }
    ]
  }
}
```

**Runners (3.0.0 bash; 5.3.0 adds host modes).** Each role runs through `driver run-stage`, which assembles the prompt from the LLM-agnostic `dp-<role>.md` + the stage's inputs. `config.runners.<role>` is an ordered array (homogeneous per role) whose entries pick an **execution mode**:

- `{ "type": "bash", "command": …, "normalizer"?: "default|passthrough" }` — a CLI invocation the driver runs (the default; **the only place an LLM is named**). Placeholders substituted: `{system_file}` `{user_file}` `{output_file}` `{project_root}` `{run_dir}` `{work_dir}`. `normalizer` applies to **json** roles only (tester/reviewer): `default` (the default) tolerates a markdown fence or surrounding prose; `passthrough` requires clean JSON. A file role (implementor/test_implementor) has no JSON output, so a normalizer on it is rejected.
- `{ "type": "subagent", "model"?: "…", "normalizer"?: … }` — the host session spawns a subagent with the assembled prompt injected (no host-specific agent file; stays LLM-free). Optional `model`. For a **json** role the handoff normalizer defaults to `default` (tolerates fences + bare JSON).
- `{ "type": "main-session", "normalizer"?: … }` — the host LLM performs the role itself (after compacting the conversation). Works even on hosts without a subagent tool.

For a bash runner the driver runs and validates; for a subagent/main-session runner it **hands the assembled prompt to the SKILL** to execute, then the SKILL validates a JSON result via `driver finalize-stage`. `llm.test_implementor` + `runners.test_implementor` are required only under TDD (default — set `tdd_mode:false` to omit). The `planner` has no runner — it runs conversationally in the host session.

> **Security:** `subagent`/`main-session` runners have **no hard tool sandbox** (LLM-free = no host agent files); their only containment is the role prose. For a read-only role (reviewer/tester) on untrusted code, prefer a **bash** runner with a scoped `--allowedTools`. Prefer `subagent`/`bash` for the reviewer so it isn't the same session that wrote the code (esp. if the implementor is `main-session`); a `main-session` reviewer is a best-effort, self-review-prone gate — acceptable only when the host can run neither a bash runner nor a subagent, and then only after compacting and warning the user. `subagent`/`main-session` are host-coupled (need a host session); `bash` is the portable default.

> **Security:** a bash runner (e.g. `claude -p` / `codex exec`) runs headless with only its scoped `--allowedTools`; `plan.md`/the contract/code are untrusted input, and for handoff modes the driver prepends a persona-switch preamble to keep the role focused. `config.json` is local, user-owned state — written **only** by the human-approved `--update-config` flow (`driver apply-config`, which validates before writing); the plan carries no config. Run dev-pipeline in a sandboxed/throwaway environment and keep each role's `--allowedTools` minimal (read-only roles use a stdout-redirect command with no `Write`). An old config with a removed runner (e.g. `spec_author`) is rejected with a hint — run `driver migrate-config --config <path>` to reset runners to `unconfigured`, then `--update-config`.

### Config fields

| Field | Required | Description |
|---|---|---|
| `driver.max_test_iteration` | Yes | Max implementation retries after test failure |
| `driver.max_review_iteration` | Yes | Max implementation retries after review failure |
| `driver.max_test_implementation_iteration` | No | Max test re-authoring when RED is not confirmed (default: 3) |
| `driver.tdd_mode` | No | Author tests first (RED→GREEN). Default `true`. Set via `--update-config` |
| `driver.run_self_evolution` | Yes | Update installed agent .md files after done (default: false) |
| `driver.review_block_severity` | No | Severities that block review pass (default: `["critical","high"]`). Null = use verdict gate |
| `llm.tester.build_instruction` | **Yes** | Exact build command. Use `"no build step"` if not needed |
| `llm.tester.install_instruction` | **Yes** | Exact install command. Use `"no install step"` if not needed |
| `llm.tester.test_instruction` | **Yes** | Exact test command. Use `"no test step"` if not needed |
| `llm.test_implementor.framework_instruction` | TDD | Test framework + where/how tests are written |
| `llm.test_implementor.test_paths` | TDD | Globs matching test files only — the role boundary (e.g. `["tests/**"]`) |
| `llm.reviewer.scope` | No | Review scope for a codex reviewer runner (if configured): `working-tree` (default), `branch`, `auto` |

---

## Usage

From a host with the skill installed (Claude Code, Cline, …), with your project open:

```
/dev-pipeline --request "add rate limiting"   # planner writes plan.md, then runs
/dev-pipeline --request "<goal>" --auto-run   # skip the post-plan approval gate
/dev-pipeline --plan plan.md                  # run an existing plan.md
/dev-pipeline --help
```

**Prerequisites:**
- `.dev-pipeline/dev-pipeline.config.json` must be present and valid
- **Commit the installed dev-pipeline files** (the canonical `.agents/skills/dev-pipeline/` tree, the `.claude/skills/dev-pipeline/` copy, and `.clinerules/workflows/dev-pipeline.md`) before running. They are tracked (not gitignored, so self-evolution can manage their history); committing them keeps dev-pipeline's own tooling out of your change's manifest — and out of a codex reviewer's `working-tree` scan, if you configure one.
- Start with a **clean working tree**. Unrelated uncommitted changes stay out of the manifest-scoped commit and review, but a clean tree keeps the role-boundary checks accurate (and out of scope for an opt-in codex reviewer, which scans the working tree).
- **Gitignore your build outputs** (compiled binaries, object files, etc.). Build/test artifacts are excluded from the change manifest by design; keeping them gitignored also keeps them out of a codex reviewer's `working-tree` scan (if you configure one).

---

## Roles

Each role is an LLM-agnostic prose file (`agents/skills/dev-pipeline/agents/dp-<role>.md`) run by `driver run-stage` through its `config.runners.<role>` command. The **tool envelope** below is whatever that command's flags grant (e.g. claude `--allowedTools`) — set in config, not in the role file.

| Role | Does | Tool envelope (set in config) |
|---|---|---|
| `dp-planner` | **Conversational, host session** (not a runner). Turns a goal into one `plan.md` spec body (no config header); read-only repo exploration | host session (read-only discipline in prose) |
| `dp-test-implementor` | (TDD) Writes tests from the contract — tests only, no production code | Read, Write, Edit (no Bash) |
| `dp-implementor` | Writes + build-checks code from the contract; never edits tests under TDD | Read, Edit, Write, Bash |
| `dp-tester` | Runs build/install/test — **no code inference** (used by `red_test` and `test`) | Read, Bash (read-only; no Write) |
| `dp-reviewer` | Adversarial review against the contract; reads the diff, never edits | Read, Grep, Glob (read-only) |

---

## Reviewer

`config.runners.reviewer` is an ordered array tried front-to-back (configured via `--update-config`; a typical choice is a **claude** reviewer — `claude -p`, read-only tools). Add more entries to get automatic fallback — the next runner is used only if one fails to produce a valid `review-result.json`. A **codex** reviewer is fully supported (`codex exec -s read-only`, OS-sandboxed) if you prefer it or want a second-vendor cross-check — just add it to the array. The reviewer reads the change diff against the spec's Acceptance Criteria.

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
    ├── contract.md          # the plan body — the contract for test author, implementor and reviewer
    ├── attempts.md          # accumulated failure history — passed to authors on retry
    ├── config.snapshot.json
    ├── changed-manifest.txt  # files the runners produced (commit/review scope)
    ├── stage-input.json      # per-stage input echoed by advance
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
python3 agents/dev-pipeline-tools/driver.py bootstrap-config --project .
python3 agents/dev-pipeline-tools/driver.py apply-config --config .dev-pipeline/dev-pipeline.config.json --values-file values.json
python3 agents/dev-pipeline-tools/driver.py validate-config --config .dev-pipeline/dev-pipeline.config.json
python3 agents/dev-pipeline-tools/driver.py status --run .dev-pipeline/latest
python3 agents/dev-pipeline-tools/driver.py resume --run .dev-pipeline/latest
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
python3 .agents/skills/dev-pipeline/driver.py --version
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
    │           ├── dp-planner.md
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
│           ├── agents/             ← role prompts (dp-planner … dp-reviewer)
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
- **Pluggable runners**: `runners` config picks an execution mode per role — a `bash` CLI (codex, gemini, …), a host `subagent` (model-selectable), or the `main-session` itself
- **Oscillation prevention**: `attempts.md` accumulates every failed attempt and is passed to the implementor on retry
- **Environment vs code failures**: tester classifies failures; environment failures halt immediately instead of retrying
- **Self-evolution**: when enabled, uses the done-state retrospective to update the installed agent `.md` files and the skill (`SKILL.md`) (source repo not updated)
