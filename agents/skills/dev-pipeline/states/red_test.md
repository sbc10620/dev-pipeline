# STATE: red_test  (TDD only)

**Goal:** Prove the freshly authored tests FAIL before any code exists (the RED phase). **A failing test run is the success condition the driver checks.** Here, `run-stage` succeeding means the tester produced a *valid result*, not that tests passed — the driver's `advance` interprets pass/fail.

The advance that landed here echoed `directive: run_tester`, `iter_dir`, and `tdd_mode`. The driver persisted the tester context (the three `*_instruction`s and a RED-phase classification note) to `<iter_dir>/stage-input.json`, with `output_file` set to `<iter_dir>/red-test-result.json`.

- [Step 1] **Run the tester:**
  ```bash
  python3 <driver_path> run-stage --run <run_dir> --role tester --stage-input <iter_dir>/stage-input.json
  ```
  The runner executes the configured build/install/test and writes a schema-valid `red-test-result.json` to `<iter_dir>`. For a bash runner, prefer running this in the background and checking `<iter_dir>/tester-runner.log` per [SKILL §Role Execution](../SKILL.md#-role-execution) if your host supports it (a quiet log there doesn't mean it's stuck — see that section for the check/relay cadence). Read the JSON:
  - **`mode` is `main-session`/`subagent`** → execute the tester per [SKILL §Role Execution](../SKILL.md#-role-execution) (json role: writes `red-test-result.json` to `output_file`; `driver finalize-stage` validates), then proceed.
  - `ok: true` → a valid result was written; proceed (its pass/fail is the driver's to interpret).
  - `ok: false` → the runner could not produce a valid result (tooling problem). Stop and report the `attempts`.

- [Step 2] Call driver advance:
  ```bash
  python3 <driver_path> advance --run <run_dir>
  ```
  The driver interprets `red-test-result.json`:
  - **tests failed (RED confirmed)** → `next_state: implementation`.
  - **tests passed (RED not confirmed)** → `next_state: test_implementation` (re-author stronger tests), or `failed` if the budget is exhausted.
  - **environment failure** → `failed` (`halt_reason: environment`).

  On a RED-not-confirmed retry (`next_state: test_implementation`) the driver **records the vacuous-tests note to `attempts.md` automatically** — you do not log it yourself.

- [Step 3] Follow `states/<next_state>.md`.

**Checklist:**
- [ ] `run-stage --role tester` returned `ok: true` (valid `red-test-result.json` written), **or** a `mode` handoff was executed and `finalize-stage` returned `ok: true`; else stopped/reported
- [ ] (bash runner, host permitting) ran in the background with the runner log checked periodically (a quiet log is expected for some runners, not a hang); relayed to the user only when there was something new to say
- [ ] `driver advance` called; followed the reported `next_state` (the driver auto-recorded any RED-not-confirmed note)
