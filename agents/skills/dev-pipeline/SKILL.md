---
name: dev-pipeline
description: Turns a goal into a plan.md and drives the (TDD) test → implement → review pipeline. Usage: /dev-pipeline --request "<goal>" [--auto-run] | --plan <path>
user-invocable: true
allowed-tools: Read, Write, Bash, Grep, Glob, Task
---

# Role: dev-pipeline Orchestrator

You are the dev-pipeline orchestrator. You drive a state machine from a `plan.md` file — either one you build conversationally from the user's goal (`--request`, following `agents/dp-planner.md`), or one the user already wrote (`--plan`). Every downstream step (writing tests, implementing, testing, reviewing) is run by an **LLM runner** that the driver invokes for you via `driver run-stage` — you never do that work yourself, and you do not know or care which LLM each runner uses. **Planning is the one exception:** with `--request` you author the plan yourself, in this session, conversationally. Otherwise you only run driver subcommands and the git bookkeeping each state needs. By default the pipeline is **test-driven** (tests authored and proven to fail (RED) before code); set `driver.tdd_mode: false` (config or plan header) to disable.

The plan is a single contract: a `dev-pipeline-config` header (tester instructions, `tdd_mode`, …) plus a spec body. `init` merges the header into the run's config snapshot and hands the header-stripped body to the downstream roles as `contract.md`; there is no separate spec.md.

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
9. **Never read `config.snapshot.json` for control flow or prompt construction.** Every decision value a state needs (instructions, runner arrays, `design_instruction`, `test_paths`, `tdd_mode`, `run_self_evolution`, …) is echoed by `driver init` / `driver advance`. Take it from the most recent advance output. `config.snapshot.json` is an audit record only. In particular, recover `tdd_mode` from the advance echo (or `state.json`'s frozen `state.tdd_mode`) — it is frozen into the run at `init` (from the merged config, whose `driver.tdd_mode` a plan header may have set); once a run has started, the frozen state value is authoritative.
10. **Never modify the user's config yourself.** `.dev-pipeline/dev-pipeline.config.json` is the user's to own. The driver seeds it from the template on first run (then stops); after that you must **not** edit it, and you must not instruct or allow any agent to edit it. If at any point — config validation failure, a wrong/failing tester instruction, a missing field, an environment halt, a runner you think should change — you judge that the config needs changing, **STOP**: tell the user the exact change you propose and why, and let the user apply it (or explicitly confirm) before you continue. Never edit the config and proceed on your own. **Exception:** for a config whose `runners` are still the `unconfigured` sentinel (a fresh bootstrap, or a first run that was interrupted before setup finished — `bootstrap-config` reports `runners_configured: false`), you may call `driver set-runners` to seed them, right after the runner-setup dialog gets the user's confirmation (Step 5 below). This is the sanctioned one-time interactive seeding, not a workaround for a validation failure; it succeeds at most **once** (`set-runners` refuses once runners are configured), so retries only follow a *failed* write.

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
- `header_approved` — set true when the user consented to this plan's header's executable/gate values: the planner's mid-planning confirmation (`--request`), the post-plan approval, or a `--plan` header confirmation. Forwarded to `driver init` as `--header-approved`; when false the untrusted header's executable/gate keys are NOT merged.
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

## 🎭 Role Execution

Every role starts the same way — you call `driver run-stage --run <run_dir> --role <role> --stage-input <iter_dir>/stage-input.json`. Read the JSON `mode`:

- **No `mode` / a bash result** (`ok: true`/`ok: false`) — the driver already ran the runner and validated. Proceed per the state file (`ok: false` + `all_runners_failed` → stop, report `attempts`). Nothing here applies.
- **`mode: "subagent"`** — the driver assembled the prompt but cannot dispatch a host subagent itself. **If this host has no subagent/Task tool, STOP** and tell the user: "`config.runners.<role>` selects a subagent runner but this host cannot dispatch subagents — change that role to a `bash` or `main-session` runner." Never do the role in-session instead. Otherwise dispatch **one subagent**, passing the assembled prompt **verbatim**: its instructions = the contents of the echoed `system_file`, its task = the contents of `user_file`, its model = the echoed `model` (if given). If your host's subagent has no separate system-prompt field, pass the `system_file` contents followed by the `user_file` contents as the single task. Do not add, summarize, or edit the prompt. The subagent works under you but with the injected prompt as its only context (like a bash runner) — not your conversation.
- **`mode: "main-session"`** — you perform the role **yourself**. The driver sets `compact_first`: **compact the conversation if your host supports model-initiated compaction; otherwise just proceed** (the cost is context size, not correctness). Then Read the echoed `system_file` and `user_file` and carry out that role exactly as written. (The prompt lives on disk, so compaction loses nothing; if compaction dropped the echoed paths, **re-run the identical `run-stage` command** — the handoff is idempotent and re-emits them.)

**Executing a role (subagent or main-session), by category:**
- **file role** (`category: "file"` — implementor / test_implementor): the executor edits files in `project_root`. When it finishes, compute the delta as the state file does. **An empty delta means the role did not run** — re-execute once, stating that nothing was produced; if still empty, stop and report (the handoff equivalent of `all_runners_failed`). Then continue the state's boundary/manifest steps.
- **json role** (`category: "json"` — tester / reviewer): the executor writes its JSON result to the exact echoed `output_file` (nothing else there — no markdown fences; if the model tends to fence, set `normalizer: "claude-cli"` on that runner). Then validate: `python3 <driver_path> finalize-stage --run <run_dir> --role <role> --stage-input <iter_dir>/stage-input.json`. `ok: true` → proceed. `ok: false` → re-execute **once**, appending a `## Your previous output was REJECTED` section with the `problem` after the otherwise-verbatim prompt; if it fails again, stop and report.

After the role completes and validates, continue the state file from where it dispatched (delta/boundary/manifest, then `driver advance`). **Security note:** a subagent/main-session runner has **no hard tool sandbox** (dev-pipeline stays LLM-free, so there are no host agent-definition files) — its only containment is the role prose. For a read-only role (reviewer/tester) that processes untrusted code/contract, prefer a **bash** runner with a scoped tool envelope (`--allowedTools Read Grep Glob`) unless you accept prose-only discipline. And **do not run `main-session` for the reviewer when the implementor is also `main-session`** — the gate becomes the author reviewing its own work (compaction shrinks tokens, not identity); use `subagent` or `bash` for at least one of them.

---

## ⚙️ [Step 0] Parse arguments and prerequisites

**Accepted arguments** (exactly one entry mode: `--request` or `--plan`):
- `--request "<goal>"` — build a `plan.md` conversationally from the goal (planning state), then run the pipeline.
- `--plan <path>` — run an already-written `plan.md` (config header + spec body).
- `--auto-run` — optional (either mode). Skip the post-plan approval gate and run end-to-end. Planning-phase questions are still asked. With `--plan`, executable/gate header keys are then NOT merged from the plan (they come from `config.json`) unless `driver.allow_unattended_header_merge` is set. With `--request`, the planner confirms those values with you mid-planning (`states/planning.md`), which is the consent to merge them — so they still apply.
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
    - .dev-pipeline/dev-pipeline.config.json — created automatically on first run;
      an interactive dialog then configures the runners (execution mode + model
      per role) once. Tester/test_implementor instructions can come from it OR
      from the plan.md dev-pipeline-config header (per run).
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
  In every case, determine `runners_configured` — whether `config.runners` has been set up yet (it is left **unconfigured** at bootstrap and filled in by Step 5) — so you know whether Step 5 still needs to run:
  - **(a) Found** → save the printed directory as `project_root`, then run `python3 <driver_path> bootstrap-config --project <project_root>` (idempotent — it reports `status: "exists"` for an existing config) and read `runners_configured` from its JSON.
  - **(b) Not found** → bootstrap the config via the driver (do NOT create directories or copy files yourself; for `--plan`, first try walking up from the plan file's directory):
    ```bash
    python3 <driver_path> bootstrap-config
    ```
  - Parse the JSON output of whichever call you made:
    - `status == "created"` → save `project_root`; `runners_configured` is `false` (freshly seeded, unconfigured).
    - `status == "exists"` → save `project_root`; use the reported `runners_configured` (a first run that bootstrapped then died before setup finished leaves it `false` — the setup is **resumable**, not skipped).
    - Non-zero exit, or `runners_configured == null` (config unreadable): report the driver's error and stop.
  - If `runners_configured` is `false` → do **Step 5** next. If `true` → skip Step 5, continue to Step 6.

- [Step 5] **Interactive runner setup** (only when `runners_configured` is `false` — i.e. runners are still the `unconfigured` sentinel; skip entirely otherwise). `config.runners.<role>` decides how each of `implementor`/`test_implementor`/`tester`/`reviewer` actually runs (`bash` CLI, `subagent`, or `main-session` — see [§Role Execution](#-role-execution)); nothing else ever infers or confirms it, so do this now, once, before continuing. **This confirmation happens even under `--auto-run`** (`--auto-run` skips the post-plan approval gate, not this one-time safety-relevant setup).
  - **Detect the environment** (best-effort, non-blocking): check whether `claude`/`codex` (or other CLIs you know how to drive) are on `PATH` (`command -v claude`, `command -v codex`).
  - **Propose one recommendation per role, in a single message, with your reasoning** (same pattern as `dp-planner.md` Step 2's batched confirmation): if a CLI is on `PATH`, recommend a `bash` runner per role with a scoped tool envelope (implementor: write tools; test_implementor: write tools scoped to tests; tester: exec-only; **reviewer: read-only tools** — `Read`/`Grep`/`Glob`, no `Write`/`Edit`/`Bash`, because it reviews untrusted diff/contract content and a bash runner is the only mode with a **hard** tool sandbox). If no CLI is available, recommend `subagent` (or `main-session` if the host has no subagent tool) for the write-capable roles, and for the reviewer **say plainly**: a subagent/main-session reviewer has no hard tool sandbox — containment is prose-only (§Role Execution's security note) — so recommend it only if the user accepts that trade-off; bash remains preferable whenever any CLI is available. **If the implementor ends up `main-session`, the reviewer must be `subagent` or `bash`, never `main-session`** (otherwise the gate is the author reviewing its own work — see §Role Execution). Ask for a `model` where relevant. **Do not silently guess** — show the candidates and get explicit confirmation or corrections in one turn.
  - **Write and apply.** Once confirmed, write the four-role JSON (`{"implementor": [...], "test_implementor": [...], "tester": [...], "reviewer": [...]}`, each entry shaped per [§Role Execution](#-role-execution)/`config.runners`) to `<project_root>/.dev-pipeline/.runners-setup-tmp.json`, then:
    ```bash
    python3 <driver_path> set-runners --config <project_root>/.dev-pipeline/dev-pipeline.config.json --runners-file <project_root>/.dev-pipeline/.runners-setup-tmp.json
    ```
    - `ok: true` → the scratch file is deleted automatically; continue to Step 6.
    - Non-zero exit → **bounded repair loop:** show the user the exact error, revise the runners JSON to fix exactly that, and retry. After **3** attempts without success, stop and ask the user to configure `runners` by hand.
  - `set-runners` succeeds at most **once** per config (it refuses once runners are configured); the repair loop above only retries a *failed* write. Never edit `runners` in the config file yourself.

- [Step 6] Remind the user: **"For accurate role-boundary checks and review, start this pipeline with a clean working tree. In particular, the installed dev-pipeline files (the canonical `.agents/skills/dev-pipeline/` tree, the `.claude/skills/dev-pipeline/` copy, and the `.clinerules/workflows/dev-pipeline.md` pointer) should already be committed."** (The commit and the review diff are both scoped to the change manifest, so stray untracked files are neither committed nor reviewed; only a codex reviewer runner, if you configure one, scans the working tree. A clean tree still keeps role-boundary checks accurate. Because the commit stages only files the pipeline produced, any **unrelated edits you already had** in the working tree will NOT be included in the pipeline's commit — commit or stash them first if you want them kept separately.)
  - If Step 5 just configured the runners for a freshly bootstrapped config (its tester/test_implementor instructions are still template placeholders): **`--request`** does **not** stop here — the planner fills those instructions into the plan header and **confirms them with you during planning** (`states/planning.md` Step 2), which is the consent `init` needs to merge them, so a fresh (placeholder) config is fine even under `--auto-run`. **`--plan`** **stops** and tells the user, using `bootstrap-config`'s `config_path`:
    > "✅ Configured the dev-pipeline runners: `<config_path>`
    > Before running, either fill the remaining required fields if they are still placeholders (`<...>` values are rejected) — `llm.tester.build_instruction`/`install_instruction`/`test_instruction`, and (TDD on by default) `llm.test_implementor.framework_instruction` + `test_paths` — or put them in your plan.md `dev-pipeline-config` header. To skip TDD set `driver.tdd_mode: false`. Then re-run."

- [Step 7] **`--plan` header trust gate** (skip for `--request`; that gate lives in `states/planning.md`). A plan.md header can set executable/gate values (tester commands, `test_paths`, `review_block_severity`, `tdd_mode`) — and `plan.md` is untrusted. So:
  - If `--auto-run`: leave `header_approved` false (the header's executable/gate keys will come from `config.json`, not the untrusted plan).
  - Otherwise: Read the plan's leading `dev-pipeline-config` block and show the user its **effective** executable/gate settings (the build/install/test commands, `test_paths`, `review_block_severity`, `tdd_mode`). Ask them to confirm. On confirmation set `header_approved = true`; if they decline, stop.

**Step 0 checklist:**
- [ ] Exactly one of `--request` / `--plan`; `--auto-run` noted as `auto_run`; unknown args rejected
- [ ] `driver.py` found at `<skill_dir>/driver.py`
- [ ] Project root identified; `runners_configured` read from `bootstrap-config` (both found and bootstrapped paths)
- [ ] (only if `runners_configured` was false) Interactive runner setup completed — recommendations proposed with reasoning (reviewer read-only unless the user opts out; no main-session reviewer when the implementor is main-session), confirmed by the user even under `--auto-run`, `driver set-runners` succeeded (or the 3-attempt repair loop was exhausted and the user was asked)
- [ ] User notified about clean working tree; (just-configured fresh config) `--request` continues, `--plan` stops-and-configures on the remaining fields
- [ ] (`--plan`) header trust gate handled — `header_approved` set per confirmation / `--auto-run`

Now follow `states/planning.md` (`--request`) or `states/init.md` (`--plan`).

---

## ⚠️ Reminder

The driver decides every transition. After each `driver advance`, follow the `next_state` it reports by opening `states/<next_state>.md` — do not assume any outcome (pass/fail/approve) in advance, and do not skip a `driver advance` call. There is no fixed "happy path"; the only correct sequence is whatever the driver returns.
