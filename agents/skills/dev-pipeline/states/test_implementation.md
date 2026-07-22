# STATE: test_implementation  (TDD only)

**Goal:** Run the test-author runner to write tests from the contract, enforce the role boundary, record the manifest, advance.

The advance that landed here echoed `directive: run_test_implementor`, `iter_dir`, `tdd_mode`, and **`work_root`**. The driver persisted the test author's context (`contract_path`, `focus`, `framework_instruction`, `test_paths`, and — on a re-entry — the red-not-confirmed note, the reviewer findings, or the implementor's concern that the tests contradict the contract) to `<iter_dir>/stage-input.json`. **All git commands below run against `work_root`, not `project_root`** — identical under a normal run, but `work_root` is the isolated worktree checkout under `--worktree` (see `states/init.md`).

- [Step 1] **Stage a boundary/manifest baseline** (git repo only — `git rev-parse --git-dir`). If not a git repo, skip the boundary guard in [Step 4] and note to the user it cannot be enforced.
  ```bash
  cd <work_root> && git add -A
  ```

- [Step 2] **Run the test author:**
  ```bash
  python3 <driver_path> run-stage --run <run_dir> --role test_implementor --stage-input <iter_dir>/stage-input.json
  ```
  The runner writes tests only (its configured tool envelope has no Bash; the driver enforces the prompt) and its (now-mandatory since 6.6.0) status JSON to the exact path the output directive gave it. For a bash runner, prefer running this in the background and checking `<iter_dir>/test_implementor-runner.log` per [SKILL §Role Execution](../SKILL.md#-role-execution) if your host supports it (a quiet log there doesn't mean it's stuck — see that section for the check/relay cadence). Read the JSON:
  - **`mode` is `main-session`/`subagent`** → execute the test author per [SKILL §Role Execution](../SKILL.md#-role-execution) (file role: the executor writes tests and its status JSON; then `driver finalize-stage` validates it), then continue with the rest of this state: [Step 3]'s result-status check runs first, then [Step 4]'s empty-delta guard catches a no-op.
  - `ok: true` → a valid status JSON was produced; proceed to [Step 3] (result-status check), then [Step 4].
  - `ok: false` → every runner failed to produce a valid result; stop and report the `attempts` (since 6.6.0 this now also covers a runner that wrote tests but failed to produce a valid status JSON, not just a nonzero exit).

- [Step 3] **Check the test author's result status — FIRST, before the empty-delta guard in [Step 4].** This file is mandatory since 6.6.0: `run-stage`/`finalize-stage` already validated it before reporting `ok: true` above, so by the time you reach this step it is guaranteed to exist and be schema-valid — read it directly with no separate validation call (same procedure as `states/implementation.md`'s equivalent step). **`status: "blocked"`**: deliberate outcome — skip [Step 4]'s empty-delta guard entirely (this covers two distinct cases per `dp-test-implementor.md`: an AC the test author could not test at all (Rule 11, `blocked_on: "contract"`), or — on a repair pass — tests it verified correct while the production code is the gap (Rule 12, `blocked_on: "implementation"`)), relay `summary`/`concern` to the user as-is ("The test author reported a blocking concern: `<concern>`. On a repair pass, `blocked_on: "implementation"` makes the driver route to the implementor to fix the production code; otherwise this may mean the plan needs revision." — the schema now requires `concern` and `blocked_on` to both be present whenever `status` is `blocked`, so `concern` is always available here), and ask whether to stop for plan revision or continue anyway. If continuing, proceed with the rest of this state as normal — the driver's [Step 5] `advance` decides the destination from `blocked_on` (do **not** route it yourself). **`status: "implemented"`**: proceed to [Step 4]'s empty-delta check as usual.

- [Step 4] **Boundary check + manifest** (skip if not a git repo). Print the delta (one `work_root`-relative path per line):
  ```bash
  { git -C <work_root> -c core.quotePath=false diff --name-only --relative; \
    git -C <work_root> -c core.quotePath=false ls-files --others --exclude-standard; } | sort -u
  ```
  **Empty-delta guard** (skip if [Step 3] found a `blocked` status): nothing printed means the author made no change — re-run [Step 2] once asking for actual tests. Otherwise, pass every printed path to check-boundary:
  ```bash
  python3 <driver_path> check-boundary --run <run_dir> --role test_implementation --changed <path1> <path2> ...
  ```
  Parse the JSON:
  - `ok: true` → record the manifest with the final delta, then [Step 5]:
    ```bash
    python3 <driver_path> record-changes --run <run_dir> --changed <path1> <path2> ...
    ```
  - `ok: false`, `reason: "no_match"` → **stop**. `test_paths` is likely misconfigured for this layout; report the message and ask the user to fix `llm.test_implementor.test_paths`. Do not loop.
  - `ok: false`, `reason: "out_of_bounds"` → revert each `violating` path (`git checkout -- <p>` tracked / `rm -f <p>` untracked), re-run [Step 2] **once**, re-check. If still `out_of_bounds`, stop and report.

- [Step 5] Call driver advance:
  ```bash
  python3 <driver_path> advance --run <run_dir>
  ```
  Then follow `states/<next_state>.md` (`red_test` in the red phase, `implementation` on a repair pass when the test author reported `status: "blocked"` + `blocked_on: "implementation"` (tests verified correct, production code is the gap), or `test` on any other repair pass).

**Checklist:**
- [ ] Baseline staged (git repos) before run-stage
- [ ] `run-stage --role test_implementor` returned `ok: true`, **or** a `mode` handoff was executed and `finalize-stage` returned `ok: true`
- [ ] (bash runner, host permitting) ran in the background with the runner log checked periodically (a quiet log is expected for some runners, not a hang); relayed to the user only when there was something new to say
- [ ] Checked the result-status file BEFORE the empty-delta guard; a `blocked` status was relayed to the user with their decision on how to proceed, and never triggered a spurious re-execute nor a manual route (the driver's `advance` picks the destination from `blocked_on`)
- [ ] Empty-delta guard applied when no `blocked` status was found
- [ ] Boundary check passed (or misconfig reported / single re-run performed); manifest recorded
- [ ] `driver advance` called; followed the reported `next_state`
