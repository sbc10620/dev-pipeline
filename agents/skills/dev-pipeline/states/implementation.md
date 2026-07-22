# STATE: implementation

**Goal:** Run the implementor runner to write (and build-check) production code, enforce the role boundary (TDD), record the manifest, advance.

The advance that landed here echoed `directive: run_implementor`, `iter_dir`, `tdd_mode`, and **`work_root`**. The driver persisted the implementor's full context (`contract_path`, `design_instruction`, `build_instruction`, `test_paths`, retry/failure context — including, when the test author rerouted here with `blocked_on: "implementation"`, its `note` that the tests were verified correct and the production code is the gap) to `<iter_dir>/stage-input.json` — you do not assemble any of it. **All git commands below run against `work_root`, not `project_root`** — identical under a normal run, but `work_root` is the isolated worktree checkout under `--worktree` (see `states/init.md`).

- [Step 1] **Stage a boundary/manifest baseline** when `work_root` is a git repo (`git rev-parse --git-dir`). This makes the git index the "before" snapshot so [Step 3] sees only the implementor's changes:
  ```bash
  cd <work_root> && git add -A
  ```

- [Step 2] **Run the implementor:**
  ```bash
  python3 <driver_path> run-stage --run <run_dir> --role implementor --stage-input <iter_dir>/stage-input.json
  ```
  The runner edits production code in `work_root`, build-checks it, and writes its (now-mandatory since 6.6.0) status JSON to the exact path the output directive gave it — this is the one permitted `.dev-pipeline/` write, alongside which the driver still enforces a bash runner's tool envelope (no test/install stages) via the configured command; you do not pass any flags. For a bash runner, prefer running this in the background and checking `<iter_dir>/implementor-runner.log` per [SKILL §Role Execution](../SKILL.md#-role-execution) if your host supports it (a quiet log there doesn't mean it's stuck — see that section for the check/relay cadence). Read the JSON:
  - **`mode` is `main-session`/`subagent`** → execute the implementor per [SKILL §Role Execution](../SKILL.md#-role-execution) (file role: the executor edits production code and writes its status JSON; then `driver finalize-stage` validates it), then continue with the rest of this state: [Step 3]'s result-status check runs first, then [Step 4]'s empty-delta guard catches a no-op with no `blocked` status.
  - `ok: true` → a valid status JSON was produced; proceed to [Step 3] (result-status check), then [Step 4].
  - `ok: false` → every runner failed to produce a valid result; stop and report the `attempts` (since 6.6.0 this now also covers a runner that edited code but failed to produce a valid status JSON, not just a nonzero exit).

- [Step 3] **Check the implementor's result status — FIRST, before the empty-delta guard in [Step 4].** This file is mandatory since 6.6.0: `run-stage`/`finalize-stage` already validated it before reporting `ok: true` above, so by the time you reach this step it is guaranteed to exist and be schema-valid — read it directly with no separate validation call:
  - **`status: "blocked"`**: a deliberate outcome — **skip the empty-delta guard in [Step 4] entirely, but still run the rest of Step 4 (boundary check, manifest) on whatever partial delta exists** before advancing; Rule 11 explicitly allows the implementor to leave partial changes, and skipping the boundary/manifest step would let those changes silently miss the commit/review scope (or, under `--worktree`, be lost entirely when the worktree is later cleaned up) and let an out-of-bounds test edit pass unchecked. Relay `summary`/`concern` to the user prominently, **wording it by `blocked_on`** (the schema requires `concern` and `blocked_on` to both be present whenever `status` is `blocked`, so `concern` is always available here):
    - `blocked_on: "contract"`: "The implementor flagged this plan as unimplementable as written: `<concern>`. You may want to revise plan.md."
    - `blocked_on: "tests"`: "The implementor believes the authored tests — not the contract — are wrong: `<concern>`. Continuing will send this to the test author to verify, not directly revise the plan."
    Ask whether to (a) stop here so they can revise the plan, or (b) continue anyway (the implementor may be wrong, or a retry with different reasoning might succeed). If continuing, run Step 4 as above, then call `driver advance` — it reads this same file and decides the next state itself: TDD + `blocked_on: "tests"` routes to `test_implementation` (the implementor believes the tests, not the contract, are at fault), everything else routes to `test` as normal (do not treat this as a routing decision you make yourself, Global Rule 1 still applies).
  - **`status: "implemented"`**: proceed to [Step 4]'s empty-delta check as usual — a claimed-implemented result with an empty delta is a genuine contradiction, not a silent pass-through.

- [Step 4] **Compute the implementor delta and record the manifest** (git repo). Print this run's delta (modified/deleted tracked + new untracked), one `work_root`-relative path per line:
  ```bash
  { git -C <work_root> -c core.quotePath=false diff --name-only --relative; \
    git -C <work_root> -c core.quotePath=false ls-files --others --exclude-standard; } | sort -u
  ```
  **Empty-delta guard** (skip if [Step 3] found a `blocked` status): nothing printed means the role did not run — re-execute [Step 2] once, stating that nothing was produced; if still empty, stop and report (the handoff equivalent of `all_runners_failed`).
  - **Boundary check — only when `tdd_mode` is true.** The implementor must not have touched test files. Pass every printed path as a separate `--changed` value:
    ```bash
    python3 <driver_path> check-boundary --run <run_dir> --role implementation --changed <path1> <path2> ...
    ```
    - `ok: true` → proceed.
    - `ok: false`, `reason: "touched_tests"` → revert each `violating` path (`git checkout -- <p>` for tracked, `rm -f <p>` for untracked), then re-run [Step 2] **once**. Re-run the print + check-boundary. If still `touched_tests`, stop and report.
  - **Record the manifest** (both modes) — using the **final, post-revert** delta:
    ```bash
    python3 <driver_path> record-changes --run <run_dir> --changed <path1> <path2> ...
    ```

- [Step 5] Call driver advance:
  ```bash
  python3 <driver_path> advance --run <run_dir>
  ```
  Then follow `states/<next_state>.md` (usually `test`, or `test_implementation` if the implementor reported the tests contradict the contract — see the reported `next_state`, never assume it).

**Checklist:**
- [ ] Baseline staged before run-stage (git repos)
- [ ] `run-stage --role implementor` returned `ok: true`, **or** a `mode` handoff was executed and `finalize-stage` returned `ok: true`; else stopped/reported
- [ ] (bash runner, host permitting) ran in the background with the runner log checked periodically (a quiet log is expected for some runners, not a hang); relayed to the user only when there was something new to say
- [ ] Checked the result-status file BEFORE the empty-delta guard; a `blocked` status was relayed to the user with their decision on how to proceed, and never triggered a spurious re-execute
- [ ] Empty-delta guard applied when no `blocked` status was found
- [ ] (TDD) boundary check passed (or single re-run performed)
- [ ] Manifest recorded with the final delta
- [ ] `driver advance` called; followed the reported `next_state`
