# STATE: test_implementation  (TDD only)

**Goal:** Run the test-author runner to write tests from the contract, enforce the role boundary, record the manifest, advance.

The advance that landed here echoed `directive: run_test_implementor`, `iter_dir`, and `tdd_mode`. The driver persisted the test author's context (`contract_path`, `focus`, `framework_instruction`, `test_paths`, and — on a re-entry — the red-not-confirmed note or the reviewer findings) to `<iter_dir>/stage-input.json`.

- [Step 1] **Stage a boundary/manifest baseline** (git repo only — `git rev-parse --git-dir`). If not a git repo, skip the boundary guard in [Step 3] and note to the user it cannot be enforced.
  ```bash
  cd <project_root> && git add -A
  ```

- [Step 2] **Run the test author:**
  ```bash
  python3 <driver_path> run-stage --run <run_dir> --role test_implementor --stage-input <iter_dir>/stage-input.json
  ```
  Read the JSON. **If `mode` is `main-session`/`subagent`, execute the test author per [SKILL §Role Execution](../SKILL.md#-role-execution)** (file role: the executor writes tests; the [Step 3] empty-delta guard catches a no-op), then continue. Otherwise `ok: true` → proceed; `ok: false` → stop and report. A bash runner writes tests only (its configured tool envelope has no Bash); the driver enforces the prompt.

- [Step 3] **Boundary check + manifest** (skip if not a git repo). Print the delta (one `project_root`-relative path per line):
  ```bash
  { git -C <project_root> -c core.quotePath=false diff --name-only --relative; \
    git -C <project_root> -c core.quotePath=false ls-files --others --exclude-standard; } | sort -u
  ```
  Pass every printed path to check-boundary:
  ```bash
  python3 <driver_path> check-boundary --run <run_dir> --role test_implementation --changed <path1> <path2> ...
  ```
  (If nothing printed, the author made no change — re-run [Step 2] once asking for actual tests.) Parse the JSON:
  - `ok: true` → record the manifest with the final delta, then [Step 4]:
    ```bash
    python3 <driver_path> record-changes --run <run_dir> --changed <path1> <path2> ...
    ```
  - `ok: false`, `reason: "no_match"` → **stop**. `test_paths` is likely misconfigured for this layout; report the message and ask the user to fix `llm.test_implementor.test_paths`. Do not loop.
  - `ok: false`, `reason: "out_of_bounds"` → revert each `violating` path (`git checkout -- <p>` tracked / `rm -f <p>` untracked), re-run [Step 2] **once**, re-check. If still `out_of_bounds`, stop and report.

- [Step 4] Call driver advance:
  ```bash
  python3 <driver_path> advance --run <run_dir>
  ```
  Then follow `states/<next_state>.md` (`red_test` in the red phase, or `test` on a repair pass).

**Checklist:**
- [ ] Baseline staged (git repos) before run-stage
- [ ] `run-stage --role test_implementor` returned `ok: true`, **or** a `mode` handoff was executed (empty-delta guard applied)
- [ ] Boundary check passed (or misconfig reported / single re-run performed); manifest recorded
- [ ] `driver advance` called; followed the reported `next_state`
