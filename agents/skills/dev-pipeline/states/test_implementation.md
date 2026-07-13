# STATE: test_implementation  (TDD only)

**Goal:** Run the test-author runner to write tests from the contract, enforce the role boundary, record the manifest, advance.

The advance that landed here echoed `directive: run_test_implementor`, `iter_dir`, `tdd_mode`, and **`work_root`**. The driver persisted the test author's context (`contract_path`, `focus`, `framework_instruction`, `test_paths`, and — on a re-entry — the red-not-confirmed note or the reviewer findings) to `<iter_dir>/stage-input.json`. **All git commands below run against `work_root`, not `project_root`** — identical under a normal run, but `work_root` is the isolated worktree checkout under `--worktree` (see `states/init.md`).

- [Step 1] **Stage a boundary/manifest baseline** (git repo only — `git rev-parse --git-dir`). If not a git repo, skip the boundary guard in [Step 3] and note to the user it cannot be enforced.
  ```bash
  cd <work_root> && git add -A
  ```

- [Step 2] **Run the test author:**
  ```bash
  python3 <driver_path> run-stage --run <run_dir> --role test_implementor --stage-input <iter_dir>/stage-input.json
  ```
  For a bash runner, prefer running this in the background and checking `<iter_dir>/test_implementor-runner.log` per [SKILL §Role Execution](../SKILL.md#-role-execution) if your host supports it (a quiet log there doesn't mean it's stuck — see that section for the check/relay cadence). Read the JSON. **If `mode` is `main-session`/`subagent`, execute the test author per [SKILL §Role Execution](../SKILL.md#-role-execution)** (file role: the executor writes tests; [Step 3]'s result-status check runs first, then the [Step 4] empty-delta guard catches a no-op), then continue. Otherwise `ok: true` → proceed; `ok: false` → stop and report. A bash runner writes tests only (its configured tool envelope has no Bash); the driver enforces the prompt.

- [Step 3] **Check the test author's result status — FIRST, before the empty-delta guard in [Step 4].** After `run-stage` returns `ok: true` (or a `mode` handoff completes), check the echoed `output_file` (or, if absent, `<iter_dir>/test_implementor-result.json`) — same procedure as `states/implementation.md`'s equivalent step:
  ```bash
  python3 <driver_path> validate-result --type test_implementor --file <path>
  ```
  **A non-zero exit here is advisory only** (capture it, e.g. with `|| true`; do not apply Global Rule 6 to this specific call) — treat "absent" or a schema violation the same way: proceed as before. **Valid, `status: "blocked"`**: deliberate outcome — skip [Step 4]'s empty-delta guard entirely, relay `summary`/`concern` to the user ("The test author flagged this plan as untestable as written: `<concern, or summary if concern is missing>`. You may want to revise plan.md." — the schema does not force `concern` to be non-null even when `blocked`, so fall back to `summary` rather than printing a blank), and ask whether to stop for plan revision or continue anyway. If continuing, proceed with the rest of this state as normal. **Valid, `status: "implemented"`** (or no result file): proceed to [Step 4]'s empty-delta check as usual.

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
  Then follow `states/<next_state>.md` (`red_test` in the red phase, or `test` on a repair pass).

**Checklist:**
- [ ] Baseline staged (git repos) before run-stage
- [ ] `run-stage --role test_implementor` returned `ok: true`, **or** a `mode` handoff was executed
- [ ] (bash runner, host permitting) ran in the background with the runner log checked periodically (a quiet log is expected for some runners, not a hang); relayed to the user only when there was something new to say
- [ ] Checked the result-status file (if present) BEFORE the empty-delta guard; a `blocked` status was relayed to the user with their decision on how to proceed, and never triggered a spurious re-execute
- [ ] Empty-delta guard applied when no `blocked` status was found
- [ ] Boundary check passed (or misconfig reported / single re-run performed); manifest recorded
- [ ] `driver advance` called; followed the reported `next_state`
