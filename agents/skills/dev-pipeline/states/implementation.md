# STATE: implementation

**Goal:** Run the implementor runner to write (and build-check) production code, enforce the role boundary (TDD), record the manifest, advance.

The advance that landed here echoed `directive: run_implementor`, `iter_dir`, and `tdd_mode`. The driver persisted the implementor's full context (`contract_path`, `design_instruction`, `build_instruction`, `test_paths`, retry/failure context) to `<iter_dir>/stage-input.json` — you do not assemble any of it.

- [Step 1] **Stage a boundary/manifest baseline** when `project_root` is a git repo (`git rev-parse --git-dir`). This makes the git index the "before" snapshot so [Step 3] sees only the implementor's changes:
  ```bash
  cd <project_root> && git add -A
  ```

- [Step 2] **Run the implementor:**
  ```bash
  python3 <driver_path> run-stage --run <run_dir> --role implementor --stage-input <iter_dir>/stage-input.json
  ```
  For a bash runner, prefer running this in the background and polling `<iter_dir>/implementor-runner.log` per [SKILL §Role Execution](../SKILL.md#-role-execution) if your host supports it. Read the JSON. **If `mode` is `main-session`/`subagent`, execute the implementor per [SKILL §Role Execution](../SKILL.md#-role-execution)** (file role: the executor edits production code; an empty [Step 3] delta means it did not run → re-execute once, else stop), then continue. Otherwise: `ok: true` → proceed; `ok: false` → stop and report (`all_runners_failed` lists the `attempts`). The runner edits production code in `project_root` and build-checks it; the driver enforces a bash runner's tool envelope (no test/install stages, no `.dev-pipeline/` edits) via the configured command — you do not pass any flags.

- [Step 3] **Compute the implementor delta and record the manifest** (git repo). Print this run's delta (modified/deleted tracked + new untracked), one `project_root`-relative path per line:
  ```bash
  { git -C <project_root> -c core.quotePath=false diff --name-only --relative; \
    git -C <project_root> -c core.quotePath=false ls-files --others --exclude-standard; } | sort -u
  ```
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

- [Step 4] Call driver advance:
  ```bash
  python3 <driver_path> advance --run <run_dir>
  ```
  Then follow `states/<next_state>.md` (it will be `test`).

**Checklist:**
- [ ] Baseline staged before run-stage (git repos)
- [ ] `run-stage --role implementor` returned `ok: true`, **or** a `mode` handoff was executed (empty-delta guard applied); else stopped/reported
- [ ] (bash runner, host permitting) ran in the background with the runner log polled for progress
- [ ] (TDD) boundary check passed (or single re-run performed)
- [ ] Manifest recorded with the final delta
- [ ] `driver advance` called; followed the reported `next_state`
