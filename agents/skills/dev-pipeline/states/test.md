# STATE: test  (GREEN)

**Goal:** Run the tester runner, advance. (Reached after implementation, or after a test-repair pass in TDD.) `run-stage` succeeding means a *valid result* was produced; the driver's `advance` interprets pass/fail.

The advance that landed here echoed `directive: run_tester` and `iter_dir`. The driver persisted the tester context (the three `*_instruction`s) to `<iter_dir>/stage-input.json`, with `output_file` set to `<iter_dir>/test-result.json`.

- [Step 1] **Run the tester:**
  ```bash
  python3 <driver_path> run-stage --run <run_dir> --role tester --stage-input <iter_dir>/stage-input.json
  ```
  The runner executes the configured build/install/test and writes a schema-valid `test-result.json` to `<iter_dir>`. Read the JSON:
  - **`mode` is `main-session`/`subagent`** → execute the tester per [SKILL §Role Execution](../SKILL.md#-role-execution) (json role: the executor runs build/install/test and writes the result to `output_file`; then `driver finalize-stage` validates it), then proceed.
  - `ok: true` → a valid result was written; proceed.
  - `ok: false` → the runner could not produce a valid result. Stop and report the `attempts`. **Do NOT run the build/install/test yourself** (Global Rule 3 — a handoff is a `mode` result, handled by the bullet above, never `ok: false`).

- [Step 2] Call driver advance:
  ```bash
  python3 <driver_path> advance --run <run_dir>
  ```
  On a code-failure retry (`next_state == "implementation"`) the driver **records the failure to `attempts.md` automatically** (`failure_details` + `log_excerpt`) — you do not log it yourself.

- [Step 3] Follow `states/<next_state>.md` (`review` on pass, `implementation` on a code failure, `failed` if exhausted/environment).

**Checklist:**
- [ ] `run-stage --role tester` returned `ok: true` (valid `test-result.json` written), **or** a `mode` handoff was executed and `finalize-stage` returned `ok: true`; else stopped/reported
- [ ] `driver advance` called; followed the reported `next_state` (the driver auto-recorded any retry failure)
