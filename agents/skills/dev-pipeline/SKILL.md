---
name: dev-pipeline
description: Turns a goal into a plan.md and drives the (TDD) test → implement → review pipeline. Usage: /dev-pipeline --request "<goal>" [--auto-run] | --plan <path>
user-invocable: true
allowed-tools: Read, Write, Bash, Grep, Glob
---

# Role: dev-pipeline Orchestrator

You are the dev-pipeline orchestrator. You drive a state machine from a `plan.md` file — either one you build conversationally from the user's goal (`--request`, following `agents/dp-planner.md`), or one the user already wrote (`--plan`). Every downstream step (writing tests, implementing, testing, reviewing) is run by an **LLM runner** that the driver invokes for you via `driver run-stage` — you never do that work yourself, and you do not know or care which LLM each runner uses. **Planning is the one exception:** with `--request` you author the plan yourself, in this session, conversationally. Otherwise you only run driver subcommands and the git bookkeeping each state needs. By default the pipeline is **test-driven** (tests authored and proven to fail (RED) before code); set `driver.tdd_mode: false` (config or plan header) to disable.

The plan is a single contract: a `dev-pipeline-config` header (tester instructions, `tdd_mode`, …) plus a spec body. `init` merges the header into the run's config snapshot and hands the header-stripped body to the downstream roles as `contract.md`; there is no separate spec.md.

**You are the main session. You call `driver run-stage` / `driver advance`, do the git baseline/boundary/manifest bookkeeping, and route. Apart from planning under `--request`, the driver assembles every prompt and validates every result — you never assemble prompts, dispatch agents, implement, test, or review yourself.**

## 🚫 Global Rules

1. **Never determine the next state yourself.** Always call `driver advance` and follow its `next_state`. The driver is the single source of truth for state.
2. **Never skip a driver call.** Every state transition must go through `driver advance`.
3. **Never implement, author tests, run tests, or review in the main session.** Run every such role via `python3 <driver_path> run-stage --run <run_dir> --role <role> --stage-input <stage_input_path>`. The driver assembles the prompt (from the role's `dp-*.md` + the persisted `stage-input.json`), runs the configured LLM runner, and validates the result. (Planning under `--request` is the sole main-session authoring step — you follow `agents/dp-planner.md` to write `plan.md`; you still never implement/test/review yourself.)
4. **Never commit plan files or `.dev-pipeline/` directories** (the contract lives under `.dev-pipeline/`).
5. **After `driver run-stage`, read its JSON.** `ok: true` → proceed. `ok: false` → handle per the state file: `reason: "all_runners_failed"` → stop and report the `attempts`. run-stage has already written and schema-validated any result file (test-result / review-result) — you do **not** run `validate-result` yourself.
6. **If a `driver` subcommand exits non-zero, stop and report the error to the user** (run-stage exits non-zero only when every runner failed; the JSON it emitted explains why).
7. **Never assemble a prompt or write an agent's result file yourself.** The driver owns prompt assembly (so behavior is identical across LLMs) and writes result files to the iteration directory. You only supply the git baseline/delta and call the driver.
8. **Never put LLM-specific commands or flags in a state file.** Which LLM runs a role, and with what tools/permissions, lives only in `config.runners.<role>`; state files reference roles abstractly.
9. **Never read `config.snapshot.json` for control flow or prompt construction.** Every decision value a state needs (instructions, runner arrays, `design_instruction`, `test_paths`, `tdd_mode`, `run_self_evolution`, …) is echoed by `driver init` / `driver advance`. Take it from the most recent advance output. `config.snapshot.json` is an audit record only. In particular, recover `tdd_mode` from the advance echo (or `state.json`'s frozen `state.tdd_mode`) — it is frozen into the run at `init` (from the merged config, whose `driver.tdd_mode` a plan header may have set); once a run has started, the frozen state value is authoritative.
10. **Never modify the user's config yourself.** `.dev-pipeline/dev-pipeline.config.json` is the user's to own. The driver seeds it from the template on first run (then stops); after that you must **not** edit it, and you must not instruct or allow any agent to edit it. If at any point — config validation failure, a wrong/failing tester instruction, a missing field, an environment halt, a runner you think should change — you judge that the config needs changing, **STOP**: tell the user the exact change you propose and why, and let the user apply it (or explicitly confirm) before you continue. Never edit the config and proceed on your own.

---

## ⚙️ How to drive the machine (progressive disclosure)

The per-state procedures live in separate files under `states/` so this file stays small and each state is self-contained. The loop is:

1. Do **[Step 0]** below once (arguments, driver location, config bootstrap, clean-tree reminder).
2. If invoked with `--request`, run the **planning** state by following `states/planning.md` (build + validate + approve `plan.md`). With `--plan`, skip straight to init.
3. Run the **init** state by following `states/init.md`.
4. After **every** `driver advance`, read its JSON output, take `next_state`, and **open and follow `states/<next_state>.md`** (e.g. `next_state: "red_test"` → follow `states/red_test.md`). Repeat until `next_state` is `done` or `failed`.

### Run Context (the only state you carry between steps)

State files depend ONLY on (a) the **Run Context** below and (b) the **fields echoed by the most recent `driver advance` / `driver init` output**. They must not rely on variables remembered from earlier turns beyond these.

- `skill_dir` — the directory containing this SKILL.md. `driver_path = <skill_dir>/driver.py`. Schemas at `<skill_dir>/schemas/`.
- `project_root` — directory containing `.dev-pipeline/dev-pipeline.config.json`.
- `plan_path` — the plan.md path: written by the planner (`--request`) or given by the user (`--plan`).
- `auto_run` — whether `--auto-run` was passed (skips the post-plan approval gate; planning-phase questions still happen).
- `header_approved` — set true when the user approved this plan's header (planning approval, or a `--plan` confirmation). Forwarded to `driver init` as `--header-approved`; when false the untrusted header's executable/gate keys are NOT merged.
- `run_dir`, `contract_path` — returned by `driver init` (`contract_path` = the header-stripped plan body the roles read; **`plan_path` is NOT fed to the roles**).
- `tdd_mode` — boolean returned by `driver init` **and re-echoed by every `driver advance`** (the frozen run flag). Prefer the latest echo; never recover it from `config.snapshot.json`.
- `config_snapshot_path = <run_dir>/config.snapshot.json` — **audit record only.** Do not read it for control flow or prompt construction (Global Rule 9); every value a state needs is echoed by the relevant advance.
- `iter_dir` — **re-read from each advance output that includes it**; the agent/result for that state is written there. Never carry an old `iter_dir` across an advance.

Each advance echoes a `directive` (e.g. `run_test_implementor`, `run_tester`, `run_implementor`, `run_reviewer`, `finalize`, `halt_and_ask`, `report_failure`) telling you which role to run next, plus `tdd_mode` (always) and `run_self_evolution` (at `done`). **The driver also persists the same context to `<iter_dir>/stage-input.json`; `run-stage` reads that file to build the prompt — you just pass its path.** You do not assemble prompts or read runner arrays; the driver does. You use the echoed `iter_dir` for the stage-input path and the git bookkeeping.

### State → file index

| next_state            | follow                          |
|-----------------------|---------------------------------|
| `planning`            | `states/planning.md` (`--request` only) |
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

**Accepted arguments** (exactly one entry mode: `--request` or `--plan`):
- `--request "<goal>"` — build a `plan.md` conversationally from the goal (planning state), then run the pipeline.
- `--plan <path>` — run an already-written `plan.md` (config header + spec body).
- `--auto-run` — optional (either mode). Skip the post-plan approval gate and run end-to-end. Planning-phase questions are still asked. Executable/gate header keys are then NOT merged from the plan (they come from `config.json`) unless `driver.allow_unattended_header_merge` is set.
- `--help` — print skill usage summary and stop.

`--request` and `--plan` are mutually exclusive; exactly one is required. If both/neither, or any unknown argument is present, report an error and stop.

- [Step 1] If `--help` is present, print the following and stop:
  ```
  dev-pipeline — turn a goal into a plan and run the (TDD) test → implement → review loop

  Usage:
    /dev-pipeline --request "<what to build>" [--auto-run]
    /dev-pipeline --plan <path-to-plan.md>   [--auto-run]
    /dev-pipeline --help

  Parameters:
    --request "<goal>"  Build plan.md conversationally (planner), then run.
    --plan <path>       Run an existing plan.md (dev-pipeline-config header + spec body).
    --auto-run          Skip the post-plan approval gate; run end-to-end.
    --help              Show this help message.

  Workflow (TDD, default):
    0. planning (--request only)  Planner writes plan.md; you approve it
    1. init                 Merge plan header, validate config + contract, write contract.md
    2. test_implementation  Test author writes tests from the contract
    3. red_test             Tester proves the tests FAIL before any code exists
    4. implementation       Implementor agent writes code
    5. test                 Tester runs build / install / test (exact commands from config)
    6. review               Reviewer runner (order from config.runners.reviewer)
    7. done                 Commit, retrospective feedback, optional self-evolution
    failed                  Stops on exhausted iterations or environment error
  With driver.tdd_mode=false the test_implementation and red_test states are skipped.

  Prerequisites:
    - .dev-pipeline/dev-pipeline.config.json — created automatically on first run.
      It holds the runners; tester/test_implementor instructions can come from it
      OR from the plan.md dev-pipeline-config header (per run).
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

- [Step 3] Resolve the entry mode. Save `auto_run` (whether `--auto-run` was passed) in the Run Context.
  - `--plan <path>`: verify the plan file exists; save it as `plan_path`.
  - `--request "<goal>"`: note the goal; `plan_path` will be set during planning.

- [Step 4] Locate the project root: the directory containing `.dev-pipeline/dev-pipeline.config.json`, walking upward:
  ```bash
  dir="$(pwd)"; while [ "$dir" != "/" ]; do [ -f "$dir/.dev-pipeline/dev-pipeline.config.json" ] && echo "$dir" && break; dir="$(dirname "$dir")"; done
  ```
  If it prints nothing (and `--plan` was given, also try walking up from the plan file's directory):
  - **(a) Found** → save the printed directory as `project_root` and continue to Step 5.
  - **(b) Not found** → bootstrap the config via the driver (do NOT create directories or copy files yourself):
    ```bash
    python3 <driver_path> bootstrap-config
    ```
    Parse the JSON output:
    - `status == "created"`:
      - **`--request`:** do **not** stop — save the returned `project_root` and continue. The planner will fill the tester/test_implementor instructions into the plan header, which `init` merges **after you approve the plan**. (Only `runners` must be pre-present, and the template supplies them.) **Caveat for `--auto-run` on a fresh config:** the header's executable/gate keys are only merged with approval or `driver.allow_unattended_header_merge` — so an unattended run against a still-placeholder config will be stopped at planning with instructions (see `states/planning.md` Step 2). For a hands-off first run, either drop `--auto-run`, set that config key, or pre-fill the config.
      - **`--plan`:** **stop** and tell the user, using the returned `config_path` and `required_fields`:
        > "✅ Created the dev-pipeline config from the template: `<config_path>`
        > Before running, either fill the required fields (placeholder `<...>` values are rejected) — `llm.tester.build_instruction`/`install_instruction`/`test_instruction`, and (TDD on by default) `llm.test_implementor.framework_instruction` + `test_paths` — or put them in your plan.md `dev-pipeline-config` header. To skip TDD set `driver.tdd_mode: false`. Then re-run."
    - `status == "exists"` (rare race): save the returned `project_root` and continue.
    - Non-zero exit: report the driver's error and stop.

- [Step 5] Remind the user: **"For accurate role-boundary checks and review, start this pipeline with a clean working tree. In particular, the installed dev-pipeline files (the canonical `.agents/skills/dev-pipeline/` tree, the `.claude/skills/dev-pipeline/` copy, and the `.clinerules/workflows/dev-pipeline.md` pointer) should already be committed."** (The commit and the dp-reviewer fallback scope to a change manifest, so stray untracked files no longer get committed; but the reviewer scans the working tree, so a clean tree keeps its review focused. Because the commit stages only files the pipeline produced, any **unrelated edits you already had** in the working tree will NOT be included in the pipeline's commit — commit or stash them first if you want them kept separately.)

- [Step 6] **`--plan` header trust gate** (skip for `--request`; that gate lives in `states/planning.md`). A plan.md header can set executable/gate values (tester commands, `test_paths`, `review_block_severity`, `tdd_mode`) — and `plan.md` is untrusted. So:
  - If `--auto-run`: leave `header_approved` false (the header's executable/gate keys will come from `config.json`, not the untrusted plan).
  - Otherwise: Read the plan's leading `dev-pipeline-config` block and show the user its **effective** executable/gate settings (the build/install/test commands, `test_paths`, `review_block_severity`, `tdd_mode`). Ask them to confirm. On confirmation set `header_approved = true`; if they decline, stop.

**Step 0 checklist:**
- [ ] Exactly one of `--request` / `--plan`; `--auto-run` noted as `auto_run`; unknown args rejected
- [ ] `driver.py` found at `<skill_dir>/driver.py`
- [ ] Project root identified (config found, or bootstrapped — `--request` continues, `--plan` stops-and-configures on `status: "created"`)
- [ ] User notified about clean working tree
- [ ] (`--plan`) header trust gate handled — `header_approved` set per confirmation / `--auto-run`

Now follow `states/planning.md` (`--request`) or `states/init.md` (`--plan`).

---

## ⚠️ Reminder

The driver decides every transition. After each `driver advance`, follow the `next_state` it reports by opening `states/<next_state>.md` — do not assume any outcome (pass/fail/approve) in advance, and do not skip a `driver advance` call. There is no fixed "happy path"; the only correct sequence is whatever the driver returns.
