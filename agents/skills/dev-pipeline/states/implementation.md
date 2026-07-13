# STATE: implementation

**Goal:** Run the implementor runner to write (and build-check) production code, enforce the role boundary (TDD), record the manifest, advance.

The advance that landed here echoed `directive: run_implementor`, `iter_dir`, `tdd_mode`, and **`work_root`**. The driver persisted the implementor's full context (`contract_path`, `design_instruction`, `build_instruction`, `test_paths`, retry/failure context) to `<iter_dir>/stage-input.json` — you do not assemble any of it. **All git commands below run against `work_root`, not `project_root`** — identical under a normal run, but `work_root` is the isolated worktree checkout under `--worktree` (see `states/init.md`).

- [Step 1] **Stage a boundary/manifest baseline** when `work_root` is a git repo (`git rev-parse --git-dir`). This makes the git index the "before" snapshot so [Step 3] sees only the implementor's changes:
  ```bash
  cd <work_root> && git add -A
  ```

- [Step 2] **Run the implementor:**
  ```bash
  python3 <driver_path> run-stage --run <run_dir> --role implementor --stage-input <iter_dir>/stage-input.json
  ```
  For a bash runner, prefer running this in the background and checking `<iter_dir>/implementor-runner.log` per [SKILL §Role Execution](../SKILL.md#-role-execution) if your host supports it (a quiet log there doesn't mean it's stuck — see that section for the check/relay cadence). Read the JSON. **If `mode` is `main-session`/`subagent`, execute the implementor per [SKILL §Role Execution](../SKILL.md#-role-execution)** (file role: the executor edits production code; [Step 3]'s result-status check runs first, then the [Step 4] empty-delta guard catches a no-op with no `blocked` status → re-execute once, else stop), then continue. Otherwise: `ok: true` → proceed; `ok: false` → stop and report (`all_runners_failed` lists the `attempts`). The runner edits production code in `work_root` and build-checks it; the driver enforces a bash runner's tool envelope (no test/install stages, no `.dev-pipeline/` edits) via the configured command — you do not pass any flags.

- [Step 3] **Check the implementor's result status — FIRST, before the empty-delta guard in [Step 4].** After `run-stage` returns `ok: true` (or a `mode` handoff completes), check the echoed `output_file` (or, if absent from the JSON, `<iter_dir>/implementor-result.json`):
  - **Absent** — proceed exactly as before (fully backward compatible; older prompts or a runner that doesn't produce this file).
  - **Present**: validate it —
    ```bash
    python3 <driver_path> validate-result --type implementor --file <path>
    ```
    **A non-zero exit here is advisory only, not a state-file failure** (capture it, e.g. with `|| true`; do not apply Global Rule 6 to this specific call) — treat a schema violation the same as "absent": note it, proceed as before.
    - **Valid, `status: "blocked"`**: this is a deliberate outcome — **skip the empty-delta guard in [Step 4] entirely.** Relay `summary`/`concern` to the user prominently: "The implementor flagged this plan as unimplementable as written: `<concern, or summary if concern is missing>`. You may want to revise plan.md." (the schema does not force `concern` to be non-null even when `blocked` — fall back to `summary` rather than printing a blank). Ask whether to (a) stop here so they can revise the plan, or (b) continue anyway (the implementor may be wrong, or a retry with different reasoning might succeed). If continuing, proceed with the rest of this state as normal (boundary check, manifest, advance) using whatever delta exists — it may be empty, and that's expected.
    - **Valid, `status: "implemented"`**: proceed to [Step 4]'s empty-delta check as usual — a claimed-implemented result with an empty delta is now a genuine contradiction, not a silent pass-through.

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
  Then follow `states/<next_state>.md` (it will be `test`).

**Checklist:**
- [ ] Baseline staged before run-stage (git repos)
- [ ] `run-stage --role implementor` returned `ok: true`, **or** a `mode` handoff was executed; else stopped/reported
- [ ] (bash runner, host permitting) ran in the background with the runner log checked periodically (a quiet log is expected for some runners, not a hang); relayed to the user only when there was something new to say
- [ ] Checked the result-status file (if present) BEFORE the empty-delta guard; a `blocked` status was relayed to the user with their decision on how to proceed, and never triggered a spurious re-execute
- [ ] Empty-delta guard applied when no `blocked` status was found
- [ ] (TDD) boundary check passed (or single re-run performed)
- [ ] Manifest recorded with the final delta
- [ ] `driver advance` called; followed the reported `next_state`
