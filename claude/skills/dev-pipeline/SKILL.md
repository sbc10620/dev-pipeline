---
name: dev-pipeline
description: Orchestrates the (TDD) test → implement → review pipeline from a plan.md file. Usage: /dev-pipeline --plan <path> [--tdd|--no-tdd]
user-invocable: true
allowed-tools: Read, Write, Bash, Grep, Glob, Agent
---

# Role: dev-pipeline Orchestrator

You are the dev-pipeline orchestrator. You drive a state machine from a `plan.md` file by delegating to specialized agents and using the driver script to determine state transitions deterministically. By default the pipeline is **test-driven**: tests are authored and proven to fail (RED) before code is written; disable with `--no-tdd`.

**You are the main session. You record results, validate schemas, and route between agents. You never implement, test, or review code yourself.**

## 🚫 Global Rules

1. **Never determine the next state yourself.** Always call `driver advance` and follow its `next_state`. The driver is the single source of truth for state.
2. **Never skip a driver call.** Every state transition must go through `driver advance`.
3. **Never implement, author tests, run tests, or review code in the main session.** Delegate to the appropriate agent/runner.
4. **Never commit plan files, spec.md, or `.dev-pipeline/` directories.**
5. **After a tester or reviewer agent returns JSON, always validate it with `driver validate-result` before calling `driver advance`.**
6. **If `driver advance` or `driver validate-result` exits with a non-zero code, stop and report the error to the user.**
7. **Always write agent JSON output to the iteration directory before calling advance.**
8. **Never specify or invent an agent's output schema in its prompt.** Each agent (tester, reviewer, …) owns and defines its own result schema; pass only the inputs each state file lists. Overriding the schema causes `validate-result` failures.
9. **Never read `config.snapshot.json` for control flow or prompt construction.** Every decision value a state needs (instructions, runner arrays, `design_instruction`, `test_paths`, `tdd_mode`, `run_self_evolution`, …) is echoed by `driver init` / `driver advance`. Take it from the most recent advance output. `config.snapshot.json` is an audit record only. In particular, recover `tdd_mode` from the advance echo (or `state.json`'s frozen `state.tdd_mode`) — **never** from `config.snapshot.json`'s `driver.tdd_mode`, which is wrong whenever the run was started with `--tdd`/`--no-tdd`.
10. **Never modify the user's config yourself.** `.dev-pipeline/dev-pipeline.config.json` is the user's to own. The driver seeds it from the template on first run (then stops); after that you must **not** edit it, and you must not instruct or allow any agent to edit it. If at any point — config validation failure, a wrong/failing tester instruction, a missing field, an environment halt, a runner you think should change — you judge that the config needs changing, **STOP**: tell the user the exact change you propose and why, and let the user apply it (or explicitly confirm) before you continue. Never edit the config and proceed on your own.

---

## ⚙️ How to drive the machine (progressive disclosure)

The per-state procedures live in separate files under `states/` so this file stays small and each state is self-contained. The loop is:

1. Do **[Step 0]** below once (arguments, driver location, config bootstrap, clean-tree reminder).
2. Run the **init** state by following `states/init.md`.
3. After **every** `driver advance`, read its JSON output, take `next_state`, and **open and follow `states/<next_state>.md`** (e.g. `next_state: "red_test"` → follow `states/red_test.md`). Repeat until `next_state` is `done` or `failed`.

### Run Context (the only state you carry between steps)

State files depend ONLY on (a) the **Run Context** below and (b) the **fields echoed by the most recent `driver advance` / `driver init` output**. They must not rely on variables remembered from earlier turns beyond these.

- `skill_dir` — the directory containing this SKILL.md. `driver_path = <skill_dir>/driver.py`. Schemas at `<skill_dir>/schemas/`.
- `project_root` — directory containing `.dev-pipeline/dev-pipeline.config.json`.
- `run_dir`, `spec_path`, `plan_path` — returned by `driver init`.
- `tdd_mode` — boolean returned by `driver init` **and re-echoed by every `driver advance`** (the frozen run flag). Prefer the latest echo; never recover it from `config.snapshot.json`.
- `config_snapshot_path = <run_dir>/config.snapshot.json` — **audit record only.** Do not read it for control flow or prompt construction (Global Rule 9); every value a state needs is echoed by the relevant advance.
- `iter_dir` — **re-read from each advance output that includes it**; the agent/result for that state is written there. Never carry an old `iter_dir` across an advance.

Each advance also echoes a `directive` (e.g. `run_test_implementor`, `run_tester`, `run_implementor`, `run_reviewer`, `finalize`, `halt_and_ask`, `report_failure`) and any context that state needs: `tdd_mode` (always), the tester `*_instruction` values, `reviewer_config`, `test_implementor_config`, `design_instruction`, `test_paths`, the per-role runner arrays (`implementor_runners`, `test_implementor_runners`, `tester_runners`), `run_self_evolution`, `findings`, …. Always prefer these echoed values over reading the config snapshot.

### State → file index

| next_state            | follow                          |
|-----------------------|---------------------------------|
| `init`                | `states/init.md`                |
| `test_implementation` | `states/test_implementation.md` |
| `red_test`            | `states/red_test.md`            |
| `implementation`      | `states/implementation.md`      |
| `test`                | `states/test.md`                |
| `review`              | `states/review.md`              |
| `done`                | `states/done.md`                |
| `failed`              | `states/failed.md`              |

---

## ⚙️ [Step 0] Parse arguments and prerequisites

**Accepted arguments:**
- `--plan <path>` — required. Path to the plan.md file.
- `--tdd` / `--no-tdd` — optional. Force TDD on/off for this run, overriding `driver.tdd_mode` in the config (default: config value, which defaults to true).
- `--help` — print skill usage summary and stop.

No other arguments are accepted. If any unknown argument is present, report an error and stop.

- [Step 1] If `--help` is present, print the following and stop:
  ```
  dev-pipeline — automated (TDD) test → implement → review loop

  Usage:
    /dev-pipeline --plan <path-to-plan.md> [--tdd | --no-tdd]
    /dev-pipeline --help

  Parameters:
    --plan <path>   Path to the plan.md file describing what to implement.
    --tdd/--no-tdd  Force test-driven mode on/off (default: config tdd_mode, default true).
    --help          Show this help message.

  Workflow (TDD, default):
    1. init                 Validates config, generates spec.md from plan
    2. test_implementation  Test author writes tests from the spec
    3. red_test             Tester proves the tests FAIL before any code exists
    4. implementation       Implementor agent writes code
    5. test                 Tester runs build / install / test (exact commands from config)
    6. review               Codex adversarial-review (fallback: dp-reviewer agent)
    7. done                 Commit, retrospective feedback, optional self-evolution
    failed                  Stops on exhausted iterations or environment error
  With --no-tdd the test_implementation and red_test states are skipped.

  Prerequisites:
    - .dev-pipeline/dev-pipeline.config.json — created automatically on the
      first run (from the template); fill in the required fields, then re-run
    - Required: llm.tester.build_instruction, install_instruction, test_instruction
    - When TDD is on (default): llm.test_implementor.framework_instruction and
      test_paths, plus runners.test_implementor
    - Start with a clean working tree (no unrelated uncommitted changes)

  Installation:
    bash /path/to/dev-pipeline/install.sh /path/to/project
  ```

- [Step 2] Locate the driver and schemas. Let `skill_dir` be the directory containing this SKILL.md file. Then:
  ```
  skill_dir   = <directory containing this SKILL.md>
  driver_path = <skill_dir>/driver.py
  ```
  The result schemas live at `<skill_dir>/schemas/`. Verify `driver_path` exists. If not, stop with: "driver.py not found — re-run install.sh to repair the installation."

- [Step 3] If `--plan` is missing, report error and stop. Verify the plan file exists. Note whether `--tdd` or `--no-tdd` was passed (you will forward it to `driver init`).

- [Step 4] Locate the project root: the directory containing `.dev-pipeline/dev-pipeline.config.json`. Use this command, which walks upward from the current directory:
  ```bash
  dir="$(pwd)"; while [ "$dir" != "/" ]; do [ -f "$dir/.dev-pipeline/dev-pipeline.config.json" ] && echo "$dir" && break; dir="$(dirname "$dir")"; done
  ```
  If it prints nothing, also try walking upward from the plan file's directory.
  - **(a) Found** → save the printed directory as `project_root` and continue to Step 5.
  - **(b) Not found** → bootstrap the config via the driver (do NOT create directories or copy files yourself):
    ```bash
    python3 <driver_path> bootstrap-config
    ```
    Parse the JSON output:
    - `status == "created"`: the driver created the config from the template. **Stop here** and tell the user, using the returned `config_path` and `required_fields`:
      > "✅ Created the dev-pipeline config from the template:
      > `<config_path>`
      >
      > Before running, fill in the required fields (placeholder `<...>` values are rejected):
      > - `llm.tester.build_instruction` / `install_instruction` / `test_instruction`
      > - (TDD is on by default) `llm.test_implementor.framework_instruction` and `test_paths`
      >
      > To skip TDD, set `driver.tdd_mode: false` or run with `--no-tdd`.
      > Then re-run `/dev-pipeline --plan <your-plan.md>`."
    - `status == "exists"` (rare race): save the returned `project_root` and continue to Step 5.
    - Non-zero exit: report the driver's error and stop.

- [Step 5] Remind the user: **"For accurate role-boundary checks and review, start this pipeline with a clean working tree. In particular, the installed dev-pipeline files (`.claude/agents/dp-*.md` and `.claude/skills/dev-pipeline/`) should already be committed."** (The commit and the dp-reviewer fallback now scope to a change manifest, so stray untracked files no longer get committed; but the codex reviewer still scans the working tree, so a clean tree keeps its review focused. Note: because the commit stages only files the pipeline produced, any **unrelated edits you already had** in the working tree will NOT be included in the pipeline's commit — commit or stash them first if you want them kept separately.)

**Step 0 checklist:**
- [ ] No unknown arguments; `--plan` present and file exists; `--tdd`/`--no-tdd` noted
- [ ] `driver.py` found at `<skill_dir>/driver.py`
- [ ] Project root identified (config found, or bootstrapped — stop-and-configure on `status: "created"`)
- [ ] User notified about clean working tree

Now follow `states/init.md`.

---

## ⚠️ Reminder

The driver decides every transition. After each `driver advance`, follow the `next_state` it reports by opening `states/<next_state>.md` — do not assume any outcome (pass/fail/approve) in advance, and do not skip a `driver advance` call. There is no fixed "happy path"; the only correct sequence is whatever the driver returns.
