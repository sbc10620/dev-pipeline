# STATE: test  (GREEN)

**Goal:** Run the tester runner, advance. (Reached after implementation, or after a test-repair pass in TDD.) `run-stage` succeeding means a *valid result* was produced; the driver's `advance` interprets pass/fail.

The advance that landed here echoed `directive: run_tester` and `iter_dir`. The driver persisted the tester context (the three `*_instruction`s) to `<iter_dir>/stage-input.json`, with `output_file` set to `<iter_dir>/test-result.json`.

- [Step 1] **Run the tester:**
  ```bash
  python3 <driver_path> run-stage --run <run_dir> --role tester --stage-input <iter_dir>/stage-input.json
  ```
  The runner executes the configured build/install/test and writes a schema-valid `test-result.json` to `<iter_dir>`. Read the JSON:
  - `ok: true` → a valid result was written; proceed.
  - `ok: false` → the runner could not produce a valid result. Stop and report the `attempts`. **Do NOT run the build/install/test yourself** (Global Rule 3).

- [Step 2] Call driver advance:
  ```bash
  python3 <driver_path> advance --run <run_dir>
  ```

- [Step 3] If `next_state == "implementation"` (test failed, retry), append the failure to attempt history **after** advance. Write `failure_details` (+ `log_excerpt`) from `<iter_dir>/test-result.json` to a temp file first:
  ```bash
  python3 <driver_path> append-attempt --run <run_dir> --state test --outcome-file <run_dir>/.attempt-tmp.md
  ```

- [Step 4] Follow `states/<next_state>.md` (`review` on pass, `implementation` on a code failure, `failed` if exhausted/environment).

**Checklist:**
- [ ] `run-stage --role tester` returned `ok: true` (valid `test-result.json` written); else stopped/reported
- [ ] `driver advance` called before any `append-attempt`; followed the reported `next_state`
