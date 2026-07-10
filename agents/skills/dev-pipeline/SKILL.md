---
name: dev-pipeline
description: Turns a goal into a plan.md and drives the (TDD) test → implement → review pipeline. Usage: /dev-pipeline --request "<goal>" [--auto-run] | --plan <path> | --update-config [<plan>]
user-invocable: true
allowed-tools: Read, Write, Bash, Grep, Glob, Task
---

# Role: dev-pipeline Orchestrator

You are the dev-pipeline orchestrator. You drive a state machine from a `plan.md` file — either one you build conversationally from the user's goal (`--request`, following `agents/dp-planner.md`), or one the user already wrote (`--plan`). Every downstream step (writing tests, implementing, testing, reviewing) is run by an **LLM runner** that the driver invokes for you via `driver run-stage` — you never do that work yourself, and you do not know or care which LLM each runner uses. **Planning is the one exception:** with `--request` you author the plan yourself, in this session, conversationally. Otherwise you only run driver subcommands and the git bookkeeping each state needs. By default the pipeline is **test-driven** (tests authored and proven to fail (RED) before code); set `driver.tdd_mode: false` (in the config, via `--update-config`) to disable.

The `plan.md` is a pure **spec body** (Requirements, Acceptance Criteria, Interface) — there is no config header. All config (runners, tester instructions, `tdd_mode`, …) lives in `config.json`, written by the `--update-config` flow. `init` freezes `config.json` into the run's snapshot and hands the whole plan body to the downstream roles as `contract.md`; there is no separate spec.md.

**You are the main session.** You call `driver run-stage` / `driver advance`, do the git baseline/boundary/manifest bookkeeping, and route. The driver always _assembles_ the prompt (from the role's `dp-*.md` + `stage-input.json`), so behavior is identical across LLMs. Who _executes_ it depends on the role's configured runner: a `bash` runner runs inside `run-stage` (the common case — you do nothing but read its result); a `main-session` or `subagent` runner makes `run-stage` hand the assembled prompt back to you to execute (you dispatch a subagent, or — after compacting — do it yourself), then you validate via `driver finalize-stage` (see [§Role Execution](#-role-execution)). **Apart from planning under `--request` and these two handoff modes, you never implement, test, or review yourself.**

## 🚫 Global Rules

1. **Never determine the next state yourself.** Always call `driver advance` and follow its `next_state`. The driver is the single source of truth for state.
2. **Never skip a driver call.** Every state transition must go through `driver advance`.
3. **Never implement, author tests, run tests, or review in the main session — unless a runner hands off to you.** Always start a role via `python3 <driver_path> run-stage --run <run_dir> --role <role> --stage-input <stage_input_path>`. For a `bash` runner the driver runs the LLM and validates; you do nothing else. **Only** when run-stage returns `mode: "main-session"` or `mode: "subagent"` do you execute the role yourself (main-session) or dispatch a subagent — following [§Role Execution](#-role-execution) exactly, never improvising. (Planning under `--request` is the other main-session authoring step — you follow `agents/dp-planner.md` to write `plan.md`.)
4. **Never commit plan files or `.dev-pipeline/` directories** (the contract lives under `.dev-pipeline/`).
5. **After `driver run-stage`, read its JSON.** A `mode` of `main-session`/`subagent` means "execute this yourself" ([§Role Execution](#-role-execution)); otherwise `ok: true` → proceed, `ok: false` with `reason: "all_runners_failed"` → stop and report the `attempts`. For a bash runner, run-stage already wrote and schema-validated the result file — you do **not** run `validate-result`. (After a main-session/subagent **json** role you DO run `driver finalize-stage` to normalize + validate — that is the handoff's equivalent, and the only time you touch validation.)
6. **If a `driver` subcommand exits non-zero, stop and report the error to the user** (run-stage exits non-zero only when every runner failed; the JSON it emitted explains why).
7. **Never assemble a prompt yourself.** The driver owns prompt assembly (so behavior is identical across LLMs); you pass the assembled `system_file`/`user_file` through unchanged. For a bash runner the driver also writes the result file. In a main-session/subagent handoff the **executor** (you, or the subagent) writes the json result to the exact `output_file` run-stage named — that is expected; you still never edit the assembled prompt or a bash runner's result.
8. **Never put LLM-specific commands or flags in a state file.** Which LLM runs a role, and with what tools/permissions, lives only in `config.runners.<role>`; state files reference roles abstractly.
9. **Never read `config.snapshot.json` for control flow or prompt construction.** Every decision value a state needs (instructions, runner arrays, `design_instruction`, `test_paths`, `tdd_mode`, `run_self_evolution`, …) is echoed by `driver init` / `driver advance`. Take it from the most recent advance output. `config.snapshot.json` is an audit record only. In particular, recover `tdd_mode` from the advance echo (or `state.json`'s frozen `state.tdd_mode`) — it is frozen into the run at `init` (from `config.json`'s `driver.tdd_mode`); once a run has started, the frozen state value is authoritative.
10. **Never modify the user's config yourself.** `.dev-pipeline/dev-pipeline.config.json` is the user's to own. The driver seeds it from the template on first run; after that you must **not** hand-edit it, and you must not instruct or allow any agent to hand-edit it. If at any point during a **run** — config validation failure, a wrong/failing tester instruction, a missing field, an environment halt, a runner you think should change — you judge that the config needs changing, **STOP**: tell the user the exact change you propose and why, and let them apply it (or re-run `--update-config`) before you continue. **Exception:** the `--update-config` flow (`states/update_config.md`) is the sanctioned config-write path — there you recommend values, get the user's approval, and call `driver apply-config` to write them. That is the ONE place config is written on the user's behalf; it validates the merged result and is re-runnable. Outside that flow, never write the config.

---

## ⚙️ How to drive the machine (progressive disclosure)

The per-state procedures live in separate files under `states/` so this file stays small and each state is self-contained. The loop is:

1. Do **[Step 0]** below once (arguments, driver location, config bootstrap, clean-tree reminder).
2. If invoked with `--update-config`, run **only** the config-setup state by following `states/update_config.md` (with `plan_path` if one was given), then **stop** (report the config is ready; the user re-invokes with `--plan`/`--request` to run the pipeline).
3. If invoked with `--request`, run the **planning** state by following `states/planning.md` (build + approve the `plan.md` spec). With `--plan`, skip straight to the config gate.
4. **Config gate:** if Step 0 reported `config_complete: false`, run the config-setup state by following `states/update_config.md` (using `plan_path`) before continuing. If `config_complete` was true, skip it.
5. Run the **init** state by following `states/init.md`.
6. After **every** `driver advance`, read its JSON output, take `next_state`, and **open and follow `states/<next_state>.md`** (e.g. `next_state: "red_test"` → follow `states/red_test.md`). Repeat until `next_state` is `done` or `failed`.

### Run Context (the only state you carry between steps)

State files depend ONLY on (a) the **Run Context** below and (b) the **fields echoed by the most recent `driver advance` / `driver init` output**. They must not rely on variables remembered from earlier turns beyond these.

- `skill_dir` — the directory containing this SKILL.md. `driver_path = <skill_dir>/driver.py`. Schemas at `<skill_dir>/schemas/`.
- `project_root` — directory containing `.dev-pipeline/dev-pipeline.config.json`.
- `plan_path` — the plan.md path: written by the planner (`--request`), or given by the user (`--plan` / `--update-config`).
- `auto_run` — whether `--auto-run` was passed (skips the post-plan approval gate; planning-phase questions still happen).
- `config_complete` — boolean from `bootstrap-config` (Step 0): true when `config.json` is ready to run (runners configured, no placeholders). When false, the config gate runs `states/update_config.md` before init.
- `run_dir`, `contract_path` — returned by `driver init` (`contract_path` = the plan body the roles read; **`plan_path` is NOT fed to the roles**).
- `tdd_mode` — boolean returned by `driver init` **and re-echoed by every `driver advance`** (the frozen run flag). Prefer the latest echo; never recover it from `config.snapshot.json`.
- `config_snapshot_path = <run_dir>/config.snapshot.json` — **audit record only.** Do not read it for control flow or prompt construction (Global Rule 9); every value a state needs is echoed by the relevant advance.
- `iter_dir` — **re-read from each advance output that includes it**; the agent/result for that state is written there. Never carry an old `iter_dir` across an advance.

Each advance echoes a `directive` (e.g. `run_test_implementor`, `run_tester`, `run_implementor`, `run_reviewer`, `finalize`, `halt_and_ask`, `report_failure`) telling you which role to run next, plus `tdd_mode` (always) and `run_self_evolution` (at `done`). **The driver also persists the same context to `<iter_dir>/stage-input.json`; `run-stage` reads that file to build the prompt — you just pass its path.** You do not assemble prompts or read runner arrays; the driver does. You use the echoed `iter_dir` for the stage-input path and the git bookkeeping.

### State → file index

| next_state            | follow                          |
|-----------------------|---------------------------------|
| `update_config`       | `states/update_config.md` (`--update-config`, or the config gate) |
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

## 🎭 Role Execution

Every role starts the same way — you call `driver run-stage --run <run_dir> --role <role> --stage-input <iter_dir>/stage-input.json`. Read the JSON `mode`:

- **No `mode` / a bash result** (`ok: true`/`ok: false`) — the driver already ran the runner and validated. Proceed per the state file (`ok: false` + `all_runners_failed` → stop, report `attempts`). Nothing here applies.
- **`mode: "subagent"`** — the driver assembled the prompt but cannot dispatch a host subagent itself. **If this host has no subagent/Task tool, STOP** and tell the user: "`config.runners.<role>` selects a subagent runner but this host cannot dispatch subagents — change that role to a `bash` or `main-session` runner." Never do the role in-session instead. Otherwise dispatch **one subagent**, passing the assembled prompt **verbatim**: its instructions = the contents of the echoed `system_file`, its task = the contents of `user_file`, its model = the echoed `model` (if given). If your host's subagent has no separate system-prompt field, pass the `system_file` contents followed by the `user_file` contents as the single task. Do not add, summarize, or edit the prompt. The subagent works under you but with the injected prompt as its only context (like a bash runner) — not your conversation.
- **`mode: "main-session"`** — you perform the role **yourself**. The driver sets `compact_first`: **compact the conversation if your host supports model-initiated compaction; otherwise just proceed** (the cost is context size, not correctness — **except for the reviewer**, where compacting away the implementation you just did is the main lever on review independence, so compact whenever you can before a `main-session` reviewer). Then **freshly Read the echoed `system_file` every time** (it carries the persona preamble the driver just re-assembled) and `user_file`, and carry out that role exactly as written — **do not act from your memory of a previous role**. (The prompt lives on disk, so compaction loses nothing; if compaction dropped the echoed paths, **re-run the identical `run-stage` command** — the handoff is idempotent and re-emits them.)

For a handoff mode the driver **prepends a firm persona-switch preamble** to the assembled `system_file` ("You are now acting SOLELY as the dev-pipeline `<role>` … disregard any prior role/context; **do ONLY the work THIS role's instructions define, then STOP — do not take on the other pipeline stages**"). Pass it through unchanged and **adopt it as the only instruction for that turn** — especially in `main-session`, where you must not let the work you just did in this session bleed into the role. Concretely: a `main-session` **implementor writes and build-checks its code (per its own instructions) and stops — it does NOT run the project's test suite** (a separate `test` stage does), and a reviewer must compact first so it is not grading its own fresh work.

**Executing a role (subagent or main-session), by category:**
- **file role** (`category: "file"` — implementor / test_implementor): the executor edits files in `project_root`. When it finishes, compute the delta as the state file does. **An empty delta means the role did not run** — re-execute once, stating that nothing was produced; if still empty, stop and report (the handoff equivalent of `all_runners_failed`). Then continue the state's boundary/manifest steps.
- **json role** (`category: "json"` — tester / reviewer): the executor writes its JSON result to the exact echoed `output_file` (nothing else there — no markdown fences; the handoff `normalizer` defaults to `default`, which tolerates a fence anyway). Then validate: `python3 <driver_path> finalize-stage --run <run_dir> --role <role> --stage-input <iter_dir>/stage-input.json`. `ok: true` → proceed. `ok: false` → re-execute **once**, appending a `## Your previous output was REJECTED` section with the `problem` after the otherwise-verbatim prompt; if it fails again, stop and report.

After the role completes and validates, **you are the orchestrator again** — resume the state file from where it dispatched (delta/boundary/manifest, then `driver advance`) and run **only** driver commands; do not keep doing role work (in `main-session` this is the moment the role/orchestrator boundary blurs — the role is done, so switch back). **Security note:** a subagent/main-session runner has **no hard tool sandbox** (dev-pipeline stays LLM-free, so there are no host agent-definition files) — its only containment is the role prose. For a read-only role (reviewer/tester) that processes untrusted code/contract, prefer a **bash** runner with a scoped tool envelope (`--allowedTools Read Grep Glob`) unless you accept prose-only discipline. **Reviewer independence:** a `main-session` reviewer after a `main-session` implementor is the author grading its own work (compaction shrinks tokens, not identity) — **prefer `subagent` or `bash` for the reviewer** so at least one of author/reviewer is independent. Only when the host can run **neither** a bash runner **nor** a subagent (so `main-session` is the reviewer's only option) is a `main-session` reviewer acceptable — and then you **must** compact first (below), rely on the reviewer prompt's independence rule (dp-reviewer.md re-frames it as an independent auditor of unknown-author code), and **warn the user** the review gate is best-effort, not independent.

---

## ⚙️ [Step 0] Parse arguments and prerequisites

**Accepted arguments** (exactly one entry mode: `--request`, `--plan`, or `--update-config`):
- `--request "<goal>"` — build a `plan.md` spec conversationally from the goal (planning state), then run the pipeline.
- `--plan <path>` — run an already-written `plan.md` (a pure spec body).
- `--update-config [<plan>]` — recommend + write `config.json` (runners, tester/test_implementor instructions, gate keys), then stop. A plan path is **optional** (it sharpens the recommendations — framework, test_paths, commands — from that plan; omit it to reconfigure from the repo + the current config). This is the only way config is written; `--plan`/`--request` auto-run it (with their plan) when the config is incomplete.
- `--auto-run` — optional (`--request`/`--plan`). Skip the post-plan approval gate and run end-to-end. Planning-phase and config-setup questions are still asked.
- `--help` — print skill usage summary and stop.

`--request`, `--plan`, and `--update-config` are mutually exclusive; exactly one is required. If more than one/none, or any unknown argument is present, report an error and stop.

- [Step 1] If `--help` is present, print the following and stop:
  ```
  dev-pipeline — turn a goal into a plan and run the (TDD) test → implement → review loop

  Usage:
    /dev-pipeline --request "<what to build>" [--auto-run]
    /dev-pipeline --plan <path-to-plan.md>   [--auto-run]
    /dev-pipeline --update-config [<path-to-plan.md>]
    /dev-pipeline --help

  Parameters:
    --request "<goal>"        Build plan.md spec conversationally (planner), then run.
    --plan <path>             Run an existing plan.md (a pure spec body).
    --update-config [<plan>]  Recommend + write config.json (plan optional), then stop.
    --auto-run              Skip the post-plan approval gate; run end-to-end.
    --help                  Show this help message.

  Workflow (TDD, default):
    0. planning (--request only)  Planner writes plan.md spec; you approve it
    1. init                 Validate config + contract, snapshot config, write contract.md
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
      Config (runners + instructions + gate keys) is set by --update-config, which
      --plan/--request auto-run when the config is incomplete.
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
  - `--update-config [<path>]`: a plan path is optional — if given, verify it exists and save it as `plan_path`; if omitted, leave `plan_path` unset (reconfigure from the repo + current config).
  - `--request "<goal>"`: note the goal; `plan_path` will be set during planning.

- [Step 4] Locate the project root: the directory containing `.dev-pipeline/dev-pipeline.config.json`, walking upward:
  ```bash
  dir="$(pwd)"; while [ "$dir" != "/" ]; do [ -f "$dir/.dev-pipeline/dev-pipeline.config.json" ] && echo "$dir" && break; dir="$(dirname "$dir")"; done
  ```
  In every case, determine `config_complete` — whether `config.json` is ready to run (runners configured **and** no placeholder instructions) — so you know whether the config gate (`states/update_config.md`) still needs to run:
  - **(a) Found** → save the printed directory as `project_root`, then run `python3 <driver_path> bootstrap-config --project <project_root>` (idempotent — it reports `status: "exists"` for an existing config) and read `config_complete` from its JSON.
  - **(b) Not found** → bootstrap the config via the driver (do NOT create directories or copy files yourself; for `--plan`/`--update-config`, first try walking up from the plan file's directory):
    ```bash
    python3 <driver_path> bootstrap-config
    ```
  - Parse the JSON output of whichever call you made:
    - `status == "created"` → save `project_root`; `config_complete` is `false` (freshly seeded).
    - `status == "exists"` → save `project_root`; use the reported `config_complete` (a first run that bootstrapped then died before setup finished leaves it `false` — the setup is **resumable**, not skipped).
    - Non-zero exit, or `config_complete == null` (config unreadable): report the driver's error and stop.
  - Save `config_complete` in the Run Context. The config gate (loop Step 4) uses it. **`--update-config` always runs `states/update_config.md` regardless of `config_complete`** (the user asked to reconfigure).

- [Step 5] Remind the user: **"For accurate role-boundary checks and review, start this pipeline with a clean working tree. In particular, the installed dev-pipeline files (the canonical `.agents/skills/dev-pipeline/` tree, the `.claude/skills/dev-pipeline/` copy, and the `.clinerules/workflows/dev-pipeline.md` pointer) should already be committed."** (The commit and the review diff are both scoped to the change manifest, so stray untracked files are neither committed nor reviewed; only a codex reviewer runner, if you configure one, scans the working tree. A clean tree still keeps role-boundary checks accurate. Because the commit stages only files the pipeline produced, any **unrelated edits you already had** in the working tree will NOT be included in the pipeline's commit — commit or stash them first if you want them kept separately.)

**Step 0 checklist:**
- [ ] Exactly one of `--request` / `--plan` / `--update-config`; `--auto-run` noted as `auto_run`; unknown args rejected
- [ ] `driver.py` found at `<skill_dir>/driver.py`
- [ ] Project root identified; `config_complete` read from `bootstrap-config` (both found and bootstrapped paths) and saved to the Run Context
- [ ] User notified about clean working tree

Now: `--update-config` → follow `states/update_config.md` and stop; `--request` → follow `states/planning.md`; `--plan` → the config gate (`states/update_config.md` if `config_complete` is false) then `states/init.md`.

---

## ⚠️ Reminder

The driver decides every transition. After each `driver advance`, follow the `next_state` it reports by opening `states/<next_state>.md` — do not assume any outcome (pass/fail/approve) in advance, and do not skip a `driver advance` call. There is no fixed "happy path"; the only correct sequence is whatever the driver returns.
