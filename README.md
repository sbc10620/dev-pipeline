# dev-pipeline

Automated **implement → test → review** pipeline for Claude Code.

Accepts a `plan.md` written by any LLM (Claude, Codex, etc.) and drives the full development cycle using specialized subagents, with deterministic state transitions handled by a Python driver script.

---

## How it works

```
plan.md
   │
   ▼
 [init]  →  validate config, generate spec.md
   │
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

- Test failure → retry implementation (up to `max_test_iteration` times)
- Review failure → retry implementation (up to `max_review_iteration` times)
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
    "run_self_evolution": false,
    "review_block_severity": ["critical", "high"]
  },
  "llm": {
    "implementor": {
      "design_instruction": "Prefer reusing existing code patterns..."
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
    "implementor": [{ "type": "claude-subagent", "agent": "dp-implementor" }],
    "tester":      [{ "type": "claude-subagent", "agent": "dp-tester" }],
    "reviewer":    [
      { "type": "codex-adversarial-review" },
      { "type": "claude-subagent", "agent": "dp-reviewer" }
    ]
  }
}
```

### Config fields

| Field | Required | Description |
|---|---|---|
| `driver.max_test_iteration` | Yes | Max implementation retries after test failure |
| `driver.max_review_iteration` | Yes | Max implementation retries after review failure |
| `driver.run_self_evolution` | Yes | Update installed agent .md files after done (default: false) |
| `driver.review_block_severity` | No | Severities that block review pass (default: `["critical","high"]`). Null = use verdict gate |
| `llm.tester.build_instruction` | **Yes** | Exact build command. Use `"no build step"` if not needed |
| `llm.tester.install_instruction` | **Yes** | Exact install command. Use `"no install step"` if not needed |
| `llm.tester.test_instruction` | **Yes** | Exact test command. Use `"no test step"` if not needed |
| `llm.reviewer.scope` | No | Codex review scope: `working-tree` (default), `branch`, `auto` |

---

## Usage

In Claude Code, with your project open:

```
/dev-pipeline --plan plan.md
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
| `dp-implementor` | sonnet | Writes code based on plan + spec | Read, Write, Edit, Bash, Grep, Glob |
| `dp-tester` | sonnet | Runs build/install/test — **no code inference** | Bash, Read only |
| `dp-reviewer` | sonnet | Adversarial code review (codex fallback) | Read, Grep, Glob only (read-only) |

---

## Reviewer: codex primary, dp-reviewer fallback

The pipeline tries `codex adversarial-review` first (using `--wait --json` for structured output).
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
    ├── spec.md              # generated from plan — shared by implementor and reviewer
    ├── attempts.md          # accumulated failure history — passed to implementor on retry
    ├── config.snapshot.json
    └── iterations/<n>/
        ├── test-result.json
        ├── review-result.json
        └── codex-raw.json
```

Each `run-id` is a timestamp (`YYYYMMDD-HHMMSS`). Previous runs are preserved for audit/debug.

---

## Driver CLI (advanced)

```bash
python3 agents/dev-pipeline-tools/driver.py --help
python3 agents/dev-pipeline-tools/driver.py validate-config --config .dev-pipeline/dev-pipeline.config.json
python3 agents/dev-pipeline-tools/driver.py status --run .dev-pipeline/latest
python3 agents/dev-pipeline-tools/driver.py normalize-review --source codex \
    --in codex-raw.json --out review-result.json
```

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
- **Self-evolution**: when enabled, uses the done-state retrospective to update installed agent `.md` files (source repo not updated)
