# dev-pipeline

Automated **test-driven** development pipeline for Claude Code: author tests from the spec, prove they fail (RED), write code, prove they pass (GREEN), then review.

Accepts a `plan.md` written by any LLM (Claude, Codex, etc.) and drives the full development cycle using specialized subagents, with deterministic state transitions handled by a Python driver script.

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

This copies agents, the skill, `driver.py`, and schemas into `<project>/.claude/` (local only) and seeds `dev-pipeline.config.json` inside the gitignored `<project>/.dev-pipeline/` directory (so it never clutters the project root or gets confused with your own source files). The pipeline runs standalone — the dev-pipeline source repo does not need to be present.

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
    "implementor":      [{ "type": "claude-subagent", "agent": "dp-implementor" }],
    "test_implementor": [{ "type": "claude-subagent", "agent": "dp-test-implementor" }],
    "tester":           [{ "type": "claude-subagent", "agent": "dp-tester" }],
    "reviewer":    [
      { "type": "codex-adversarial-review" },
      { "type": "claude-subagent", "agent": "dp-reviewer" }
    ]
  }
}
```

`llm.test_implementor` and `runners.test_implementor` are required only when TDD is enabled (the default). Set `driver.tdd_mode: false` or run `--no-tdd` to omit them.

### Config fields

| Field | Required | Description |
|---|---|---|
| `driver.max_test_iteration` | Yes | Max implementation retries after test failure |
| `driver.max_review_iteration` | Yes | Max implementation retries after review failure |
| `driver.max_test_implementation_iteration` | No | Max test re-authoring when RED is not confirmed (default: 3 if omitted; template seeds 2) |
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
- **Commit the installed dev-pipeline files** (`.claude/agents/dp-*.md`, `.claude/skills/dev-pipeline/`) before running. They are tracked (not gitignored, so self-evolution can manage their history), and the review uses `working-tree` scope — committing them keeps the reviewer focused on your code instead of dev-pipeline's own tooling.
- Start with a **clean working tree** (no unrelated uncommitted changes — they will be included in the review scope)
- **Gitignore your build outputs** (compiled binaries, object files, etc.). The review uses `working-tree` scope, so any untracked artifact produced by the test phase would otherwise be reviewed alongside your real changes.

---

## Agents

| Agent | Model | Role | Permissions |
|---|---|---|---|
| `dp-test-implementor` | sonnet | (TDD) Writes tests from the spec — tests only, no production code | Read, Write, Edit, Grep, Glob |
| `dp-implementor` | sonnet | Writes code based on plan + spec; never edits tests under TDD | Read, Write, Edit, Bash, Grep, Glob |
| `dp-tester` | sonnet | Runs build/install/test — **no code inference** (used by `red_test` and `test`) | Bash, Read only |
| `dp-reviewer` | sonnet | Adversarial code review, incl. test code; never runs it (codex fallback) | Read, Grep, Glob only (read-only) |

---

## Reviewer: codex primary, dp-reviewer fallback

The pipeline tries `codex adversarial-review` first (using `--wait --json` for structured output).
The spec is passed through codex's focus text so it reviews against the spec's Acceptance Criteria.
Falls back to `dp-reviewer` subagent if:
- Codex plugin not installed
- Usage limit reached
- Output parsing fails

Fallback is reported to the user when it occurs.

---

## Review gate

By default, findings with `critical` or `high` severity block the review pass.
Configure with `driver.review_block_severity`. Set to `null` to use verdict-based gating instead.

---

## Runtime directory

Created at `<project>/.dev-pipeline/` (gitignored automatically).

```
.dev-pipeline/
├── dev-pipeline.config.json # your config — seeded by install.sh, lives here (gitignored)
├── latest -> runs/<run-id>
└── runs/<run-id>/
    ├── state.json           # driver state (single source of truth)
    ├── spec.md              # generated from plan — shared by test author, implementor and reviewer
    ├── attempts.md          # accumulated failure history — passed to authors on retry
    ├── config.snapshot.json
    └── iterations/<n>/
        ├── red-test-result.json   # TDD: the red_test (RED verification) result
        ├── test-result.json
        ├── review-result.json
        └── codex-raw.json
```

Each `run-id` is a timestamp (`YYYYMMDD-HHMMSS`). Previous runs are preserved for audit/debug.

---

## Driver CLI (advanced)

```bash
python3 agents/dev-pipeline-tools/driver.py --help
python3 agents/dev-pipeline-tools/driver.py --version
python3 agents/dev-pipeline-tools/driver.py validate-config --config .dev-pipeline/dev-pipeline.config.json
python3 agents/dev-pipeline-tools/driver.py status --run .dev-pipeline/latest
python3 agents/dev-pipeline-tools/driver.py normalize-review --source codex \
    --in codex-raw.json --out review-result.json
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
├── claude/
│   ├── agents/
│   │   ├── dp-implementor.md
│   │   ├── dp-tester.md
│   │   └── dp-reviewer.md
│   └── skills/
│       └── dev-pipeline/
│           └── SKILL.md
└── agents/
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
└── .claude/
    ├── agents/
    │   ├── dp-implementor.md
    │   ├── dp-tester.md
    │   └── dp-reviewer.md
    └── skills/
        └── dev-pipeline/
            ├── SKILL.md
            ├── driver.py           ← installed for standalone operation
            └── schemas/
                ├── config.schema.json
                ├── test-result.schema.json
                ├── review-result.schema.json
                └── state.schema.json
```

---

## Design notes

- **Deterministic state**: all state transitions go through `driver.py` — the LLM never decides the next state
- **Pluggable runners**: `runners` config is an ordered array of backends; add `bash` runner for other CLIs (e.g., cline)
- **Oscillation prevention**: `attempts.md` accumulates every failed attempt and is passed to the implementor on retry
- **Environment vs code failures**: tester classifies failures; environment failures halt immediately instead of retrying
- **Self-evolution**: when enabled, uses the done-state retrospective to update the installed agent `.md` files and the skill (`SKILL.md`) (source repo not updated)
